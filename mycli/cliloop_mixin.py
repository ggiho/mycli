"""Interactive CLI loop mixin for MyCli."""
from __future__ import annotations

import os
import sys
import traceback
from time import time
from typing import TYPE_CHECKING, Generator

import click
import pymysql
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory  # noqa: F401 (kept for backwards compat)
from prompt_toolkit.completion import DynamicCompleter
from prompt_toolkit.enums import DEFAULT_BUFFER, EditingMode
from prompt_toolkit.filters import HasFocus, IsDone
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText
from prompt_toolkit.layout.processors import ConditionalProcessor, HighlightMatchingBracketProcessor
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.shortcuts import CompleteStyle, PromptSession
from pymysql.cursors import Cursor

from mycli import __version__
from mycli.clibuffer import cli_is_multiline
from mycli.clistyle import style_factory
from mycli.clitoolbar import create_toolbar_tokens_func
from mycli.constants import DEFAULT_WIDTH, Query, SUPPORT_INFO
from mycli.key_bindings import mycli_bindings
from mycli.lexer import MyCliLexer
from mycli.packages import special
from mycli.packages.filepaths import dir_path_exists
from mycli.packages.hybrid_redirection import get_redirect_components, is_redirect_command
from mycli.packages.parseutils import is_dropping_database
from mycli.packages.prompt_utils import confirm, confirm_destructive_query
from mycli.packages.sqlresult import SQLResult
from mycli.packages.toolkit.history import FileHistoryWithTimestamp, FrequencyWeightedAutoSuggest
from mycli.query_utils import completion_refresh_scope, is_mutating, is_select, need_completion_refresh, need_completion_reset

if TYPE_CHECKING:
    from mycli.sqlexecute import SQLExecute


def thanks_picker() -> str:
    from importlib import resources
    from random import choice
    import re
    import mycli

    lines: str = ""
    with resources.files(mycli).joinpath("AUTHORS").open('r') as f:
        lines += f.read()

    with resources.files(mycli).joinpath("SPONSORS").open('r') as f:
        lines += f.read()

    contents = []
    for line in lines.split("\n"):
        if m := re.match(r"^ *\* (.*)", line):
            contents.append(m.group(1))
    return choice(contents) if contents else 'our sponsors'


class CLILoopMixin:
    """Mixin providing the interactive CLI loop for MyCli."""

    def handle_editor_command(self, text: str) -> str:
        r"""Editor command is any query that is prefixed or suffixed by a '\e'.
        The reason for a while loop is because a user might edit a query
        multiple times. For eg:

        "select * from \e"<enter> to edit it in vim, then come
        back to the prompt with the edited query "select * from
        blah where q = 'abc'\e" to edit it again.
        :param text: Document
        :return: Document

        """

        while special.editor_command(text):
            filename = special.get_filename(text)
            query = special.get_editor_query(text) or self.get_last_query()
            sql, message = special.open_external_editor(filename=filename, sql=query)
            if message:
                # Something went wrong. Raise an exception and bail.
                raise RuntimeError(message)
            while True:
                try:
                    if self.prompt_app is None:
                        raise RuntimeError("Prompt application not initialized")
                    text = self.prompt_app.prompt(default=sql)
                    break
                except KeyboardInterrupt:
                    sql = ""

            continue
        return text

    def handle_clip_command(self, text: str) -> bool:
        r"""A clip command is any query that is prefixed or suffixed by a
        '\clip'.

        :param text: Document
        :return: Boolean

        """

        if special.clip_command(text):
            query = special.get_clip_query(text) or self.get_last_query()
            message = special.copy_query_to_clipboard(sql=query)
            if message:
                raise RuntimeError(message)
            return True
        return False

    def handle_prettify_binding(self, text: str) -> str:
        import sqlglot  # Lazy import - only loaded when prettify is triggered

        try:
            statements = sqlglot.parse(text, read="mysql")
        except Exception as e:
            self.logger.debug("sqlglot parse failed: %r", e)
            statements = []
        if len(statements) == 1 and statements[0]:
            pretty_text = statements[0].sql(pretty=True, pad=4, dialect="mysql")
        else:
            pretty_text = ""
            self.toolbar_error_message = "Prettify failed to parse statement"
        if len(pretty_text) > 0:
            pretty_text = pretty_text + ";"
        return pretty_text

    def handle_unprettify_binding(self, text: str) -> str:
        import sqlglot  # Lazy import - only loaded when unprettify is triggered

        try:
            statements = sqlglot.parse(text, read="mysql")
        except Exception as e:
            self.logger.debug("sqlglot parse failed: %r", e)
            statements = []
        if len(statements) == 1 and statements[0]:
            unpretty_text = statements[0].sql(pretty=False, dialect="mysql")
        else:
            unpretty_text = ""
            self.toolbar_error_message = "Unprettify failed to parse statement"
        if len(unpretty_text) > 0:
            unpretty_text = unpretty_text + ";"
        return unpretty_text

    def run_cli(self) -> None:
        iterations = 0
        sqlexecute: 'SQLExecute' = self.sqlexecute
        if sqlexecute is None:
            raise RuntimeError("SQLExecute instance not initialized")
        logger = self.logger
        self.configure_pager()

        if self.smart_completion:
            self.refresh_completions()

        history_file = os.path.expanduser(os.environ.get("MYCLI_HISTFILE", self.config.get("history_file", "~/.mycli-history")))
        if dir_path_exists(history_file):
            history = FileHistoryWithTimestamp(history_file)
        else:
            history = None
            self.echo(
                f'Error: Unable to open the history file "{history_file}". Your query history will not be saved.',
                err=True,
                fg="red",
            )

        key_bindings = mycli_bindings(self)

        if not self.less_chatty:
            print(sqlexecute.server_info)
            print("mycli", __version__)
            print(SUPPORT_INFO)
            print("Thanks to the contributor -", thanks_picker())

        def get_message() -> ANSI:
            prompt = self.get_prompt(self.prompt_format)
            if self.prompt_format == self.default_prompt and len(prompt) > self.max_len_prompt:
                prompt = self.get_prompt(self.default_prompt_splitln)
            prompt = prompt.replace("\\x1b", "\x1b")
            return ANSI(prompt)

        def get_continuation(width: int, _two: int, _three: int) -> AnyFormattedText:
            if self.multiline_continuation_char == "":
                continuation = ""
            elif self.multiline_continuation_char:
                left_padding = width - len(self.multiline_continuation_char)
                continuation = " " * max((left_padding - 1), 0) + self.multiline_continuation_char + " "
            else:
                continuation = " "
            return [("class:continuation", continuation)]

        def show_suggestion_tip() -> bool:
            return iterations < 2

        # Keep track of whether or not the query is mutating. In case
        # of a multi-statement query, the overall query is considered
        # mutating if any one of the component statements is mutating
        mutating = False

        def output_res(results: Generator[SQLResult, None, None], start: float) -> None:
            nonlocal mutating
            result_count = 0
            for result in results:
                title = result.title
                cur = result.results
                headers = result.headers
                status = result.status
                command = result.command
                logger.debug("title: %r", title)
                logger.debug("headers: %r", headers)
                logger.debug("rows: %r", cur)
                logger.debug("status: %r", status)
                threshold = 1000
                # If this is a watch query, offset the start time on the 2nd+ iteration
                # to account for the sleep duration
                if command is not None and command["name"] == "watch":
                    if result_count > 0:
                        try:
                            watch_seconds = float(command["seconds"])
                            start += watch_seconds
                        except ValueError as e:
                            self.echo(f"Invalid watch sleep time provided ({e}).", err=True, fg="red")
                            sys.exit(1)
                if is_select(status) and isinstance(cur, Cursor) and cur.rowcount > threshold:
                    self.echo(
                        f"The result set has more than {threshold} rows.",
                        fg="red",
                    )
                    if not confirm("Do you want to continue?"):
                        self.echo("Aborted!", err=True, fg="red")
                        break

                if self.auto_vertical_output:
                    if self.prompt_app is not None:
                        max_width = self.prompt_app.output.get_size().columns
                    else:
                        max_width = DEFAULT_WIDTH
                else:
                    max_width = None

                # Capture result for \last / \copy (only for manageable result sets)
                if is_select(status) and headers and isinstance(cur, Cursor):
                    try:
                        rows = list(cur)
                        special.store_last_result(headers, rows)
                        cur = iter(rows)
                    except Exception:
                        pass

                formatted = self.format_output(
                    title,
                    cur,
                    headers,
                    special.is_expanded_output(),
                    special.is_redirected(),
                    self.null_string,
                    self.numeric_alignment,
                    max_width,
                )

                t = time() - start
                try:
                    if result_count > 0:
                        self.echo("")
                    try:
                        self.output(formatted, status)
                    except KeyboardInterrupt:
                        pass
                    if self.beep_after_seconds > 0 and t >= self.beep_after_seconds:
                        self.bell()
                    if special.is_timing_enabled():
                        self.echo(f"Time: {t:0.03f}s")
                except KeyboardInterrupt:
                    pass

                start = time()
                result_count += 1
                mutating = mutating or is_mutating(status)

                # get and display warnings if enabled
                if self.show_warnings and isinstance(cur, Cursor) and cur.warning_count > 0:
                    warnings = sqlexecute.run("SHOW WARNINGS")
                    for warning in warnings:
                        title = warning.title
                        cur = warning.results
                        headers = warning.headers
                        status = warning.status
                        formatted = self.format_output(
                            title,
                            cur,
                            headers,
                            special.is_expanded_output(),
                            special.is_redirected(),
                            self.null_string,
                            self.numeric_alignment,
                            max_width,
                        )
                        self.echo("")
                        self.output(formatted, status)

        def one_iteration(text: str | None = None) -> None:
            nonlocal mutating
            successful = False
            if text is None:
                try:
                    if self.prompt_app is None:
                        raise RuntimeError("Prompt application not initialized")
                    text = self.prompt_app.prompt()
                except KeyboardInterrupt:
                    return

                special.set_expanded_output(False)
                special.set_forced_horizontal_output(False)

                try:
                    text = self.handle_editor_command(text)
                except RuntimeError as e:
                    logger.error("sql: %r, error: %r", text, e)
                    logger.error("traceback: %r", traceback.format_exc())
                    self.echo(str(e), err=True, fg="red")
                    return

                try:
                    if self.handle_clip_command(text):
                        return
                except RuntimeError as e:
                    logger.error("sql: %r, error: %r", text, e)
                    logger.error("traceback: %r", traceback.format_exc())
                    self.echo(str(e), err=True, fg="red")
                    return
                # LLM command support
                while special.is_llm_command(text):
                    start = time()
                    try:
                        if sqlexecute.conn is None:
                            raise RuntimeError("Database connection not established")
                        cur = sqlexecute.conn.cursor()
                        context, sql, duration = special.handle_llm(text, cur)
                        if context:
                            click.echo("LLM Response:")
                            click.echo(context)
                            click.echo("---")
                        if special.is_timing_enabled():
                            click.echo(f"Time: {duration:.2f} seconds")

                        # Check if the generated SQL contains dangerous keywords
                        if sql:
                            sql_upper = sql.upper()
                            dangerous_keywords = ['DELETE', 'DROP', 'UPDATE', 'TRUNCATE', 'ALTER']
                            for keyword in dangerous_keywords:
                                if keyword in sql_upper:
                                    click.secho(
                                        f"⚠️  Warning: The generated SQL contains '{keyword}'. Please review carefully before executing.",
                                        fg="yellow",
                                        err=True
                                    )
                                    break

                        text = self.prompt_app.prompt(default=sql or '')
                    except KeyboardInterrupt:
                        return
                    except special.FinishIteration as e:
                        if e.results:
                            return output_res(e.results, start)
                        else:
                            return None
                    except RuntimeError as e:
                        logger.error("sql: %r, error: %r", text, e)
                        logger.error("traceback: %r", traceback.format_exc())
                        self.echo(str(e), err=True, fg="red")
                        return

            text = text.strip()

            if not text:
                return

            if is_redirect_command(text):
                sql_part, command_part, file_operator_part, file_part = get_redirect_components(text)
                text = sql_part or ''
                try:
                    special.set_redirect(command_part, file_operator_part, file_part)
                except (FileNotFoundError, OSError, RuntimeError) as e:
                    logger.error("sql: %r, error: %r", text, e)
                    logger.error("traceback: %r", traceback.format_exc())
                    self.echo(str(e), err=True, fg="red")
                    return

            if self.destructive_warning:
                destroy = confirm_destructive_query(self.destructive_keywords, text)
                if destroy is None:
                    pass  # Query was not destructive. Nothing to do here.
                elif destroy is True:
                    self.echo("Your call!")
                else:
                    self.echo("Wise choice!")
                    return
            else:
                destroy = True

            try:
                max_reconnect_attempts = 2
                for attempt in range(max_reconnect_attempts + 1):
                    try:
                        logger.debug("sql: %r", text)

                        special.write_tee(self.get_prompt(self.prompt_format) + text)
                        self.log_query(text)

                        successful = False
                        start = time()
                        res = sqlexecute.run(text)
                        self.main_formatter.query = text
                        self.redirect_formatter.query = text
                        successful = True
                        output_res(res, start)
                        special.unset_once_if_written(self.post_redirect_command)
                        special.flush_pipe_once_if_written(self.post_redirect_command)
                        break  # Success — exit retry loop
                    except pymysql.err.InterfaceError:
                        if attempt >= max_reconnect_attempts or not self.reconnect():
                            return
                        # Retry the query after successful reconnect
                        continue
                    except pymysql.OperationalError as e1:
                        logger.debug("Exception: %r", e1)
                        if e1.args[0] in (2003, 2006, 2013):
                            # attempt to reconnect
                            if attempt >= max_reconnect_attempts or not self.reconnect():
                                return
                            # Retry the query after successful reconnect
                            continue
                        else:
                            # Re-raise for outer handler to deal with non-reconnectable errors
                            raise
            except EOFError:
                raise
            except KeyboardInterrupt:
                # get last connection id
                connection_id_to_kill = sqlexecute.connection_id or 0
                # some mysql compatible databases may not implemente connection_id()
                if connection_id_to_kill > 0:
                    logger.debug("connection id to kill: %r", connection_id_to_kill)
                    # Restart connection to the database
                    sqlexecute.connect()
                    try:
                        for _title, _cur, _headers, status in sqlexecute.run(f"kill {connection_id_to_kill}"):
                            status_str = str(status).lower()
                            if status_str.find("ok") > -1:
                                logger.debug("cancelled query, connection id: %r, sql: %r", connection_id_to_kill, text)
                                self.echo(f"Cancelled query id: {connection_id_to_kill}", err=True, fg="blue")
                            else:
                                logger.debug(
                                    "Failed to confirm query cancellation, connection id: %r, sql: %r",
                                    connection_id_to_kill,
                                    text,
                                )
                                self.echo(f"Failed to confirm query cancellation, id: {connection_id_to_kill}", err=True, fg="red")
                    except Exception as e:
                        self.echo(f"Encountered error while cancelling query: {e}", err=True, fg="red")
                else:
                    logger.debug("Did not get a connection id, skip cancelling query")
                    self.echo("Did not get a connection id, skip cancelling query", err=True, fg="red")
            except NotImplementedError:
                self.echo("Not Yet Implemented.", fg="yellow")
            except pymysql.OperationalError as e1:
                # Only non-reconnectable OperationalErrors reach here
                # (reconnectable ones are handled in retry loop)
                logger.error("sql: %r, error: %r", text, e1)
                logger.error("traceback: %r", traceback.format_exc())
                self.echo(str(e1), err=True, fg="red")
            except Exception as e:
                logger.error("sql: %r, error: %r", text, e)
                logger.error("traceback: %r", traceback.format_exc())
                self.echo(str(e), err=True, fg="red")
            else:
                if is_dropping_database(text, sqlexecute.dbname):
                    sqlexecute.dbname = None
                    sqlexecute.connect()

                # Refresh the table names and column names if necessary.
                if need_completion_refresh(text):
                    scope = completion_refresh_scope(text)
                    self.refresh_completions(reset=need_completion_reset(text), only=scope)
            finally:
                if self.logfile is False:
                    self.echo("Warning: This query was not logged.", err=True, fg="red")
            query = Query(text, successful, mutating)
            self.query_history.append(query)

        get_toolbar_tokens = create_toolbar_tokens_func(self, show_suggestion_tip)
        if self.wider_completion_menu:
            complete_style = CompleteStyle.MULTI_COLUMN
        else:
            complete_style = CompleteStyle.COLUMN

        with self._completer_lock:
            if self.key_bindings == "vi":
                editing_mode = EditingMode.VI
            else:
                editing_mode = EditingMode.EMACS

            self.prompt_app = PromptSession(
                lexer=PygmentsLexer(MyCliLexer),
                reserve_space_for_menu=self.get_reserved_space(),
                message=get_message,
                prompt_continuation=get_continuation,
                bottom_toolbar=get_toolbar_tokens,
                complete_style=complete_style,
                input_processors=[
                    ConditionalProcessor(
                        processor=HighlightMatchingBracketProcessor(chars="[](){}"), filter=HasFocus(DEFAULT_BUFFER) & ~IsDone()
                    )
                ],
                tempfile_suffix=".sql",
                completer=DynamicCompleter(lambda: self.completer),
                history=history,
                auto_suggest=FrequencyWeightedAutoSuggest(),
                complete_while_typing=True,
                multiline=cli_is_multiline(self),
                style=style_factory(self.syntax_style, self.cli_style),
                include_default_pygments_style=False,
                key_bindings=key_bindings,
                enable_open_in_editor=True,
                enable_system_prompt=True,
                enable_suspend=True,
                editing_mode=editing_mode,
                search_ignore_case=True,
            )

        try:
            while True:
                one_iteration()
                iterations += 1
        except EOFError:
            special.close_tee()
            if not self.less_chatty:
                self.echo("Goodbye!")
