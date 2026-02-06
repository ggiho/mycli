"""Output formatting and display mixin for MyCli."""
from __future__ import annotations

import itertools
import os
import shutil
from datetime import datetime
from decimal import Decimal
from io import TextIOWrapper
from typing import TYPE_CHECKING, Generator, Iterable

import click
from cli_helpers.tabular_output import preprocessors
from cli_helpers.tabular_output.output_formatter import MISSING_VALUE as DEFAULT_MISSING_VALUE
from cli_helpers.utils import strip_ansi
from prompt_toolkit.document import Document
from pymysql.cursors import Cursor

from mycli.compat import WIN
from mycli.constants import DEFAULT_HEIGHT, DEFAULT_WIDTH
from mycli.packages import special
from mycli.packages.tabular_output import sql_format
from mycli.packages.sqlresult import SQLResult
from mycli.sqlexecute import FIELD_TYPES

if TYPE_CHECKING:
    from mycli.sqlcompleter import SQLCompleter


class OutputMixin:
    """Mixin providing output formatting and display methods for MyCli."""

    def echo(self, s: str, **kwargs) -> None:
        """Print a message to stdout.

        The message will be logged in the audit log, if enabled.

        All keyword arguments are passed to click.echo().

        """
        self.log_output(s)
        click.secho(s, **kwargs)

    def bell(self) -> None:
        """Print a bell on the stderr."""
        click.secho("\a", err=True, nl=False)

    def get_output_margin(self, status: str | None = None) -> int:
        """Get the output margin (number of rows for the prompt, footer and
        timing message."""
        margin = self.get_reserved_space() + self.get_prompt(self.prompt_format).count("\n") + 1
        if special.is_timing_enabled():
            margin += 1
        if status:
            margin += 1 + status.count("\n")

        return margin

    def output(self, output: itertools.chain[str], status: str | None = None) -> None:
        """Output text to stdout or a pager command.

        The status text is not outputted to pager or files.

        The message will be logged in the audit log, if enabled. The
        message will be written to the tee file, if enabled. The
        message will be written to the output file, if enabled.

        """
        if output:
            if self.prompt_app is not None:
                size = self.prompt_app.output.get_size()
                size_columns = size.columns
                size_rows = size.rows
            else:
                size_columns = DEFAULT_WIDTH
                size_rows = DEFAULT_HEIGHT

            margin = self.get_output_margin(status)

            fits = True
            buf = []
            output_via_pager = self.explicit_pager and special.is_pager_enabled()
            for i, line in enumerate(output, 1):
                self.log_output(line)
                special.write_tee(line)
                special.write_once(line)
                special.write_pipe_once(line)

                if special.is_redirected():
                    pass
                elif fits or output_via_pager:
                    # buffering
                    buf.append(line)
                    if len(line) > size_columns or i > (size_rows - margin):
                        fits = False
                        if not self.explicit_pager and special.is_pager_enabled():
                            # doesn't fit, use pager
                            output_via_pager = True

                        if not output_via_pager:
                            # doesn't fit, flush buffer
                            for buf_line in buf:
                                click.secho(buf_line)
                            buf = []
                else:
                    click.secho(line)

            if buf:
                if output_via_pager:

                    def newlinewrapper(text: list[str]) -> Generator[str, None, None]:
                        for line in text:
                            yield line + "\n"

                    click.echo_via_pager(newlinewrapper(buf))
                else:
                    for line in buf:
                        click.secho(line)

        if status:
            self.log_output(status)
            click.secho(status)

    def configure_pager(self) -> None:
        # Provide sane defaults for less if they are empty.
        if not os.environ.get("LESS"):
            os.environ["LESS"] = "-RXF"

        cnf = self.read_my_cnf(self.my_cnf, ["pager", "skip-pager"])
        cnf_pager = cnf["pager"] or self.config["main"]["pager"]

        # help Windows users who haven't edited the default myclirc
        if WIN and cnf_pager == 'less' and not shutil.which(cnf_pager):
            cnf_pager = 'more'

        if cnf_pager:
            special.set_pager(cnf_pager)
            self.explicit_pager = True
        else:
            self.explicit_pager = False

        if cnf["skip-pager"] or not self.config["main"].as_bool("enable_pager"):
            special.disable_pager()

    def refresh_completions(self, reset: bool = False) -> list[SQLResult]:
        if reset:
            with self._completer_lock:
                self.completer.reset_completions()
        assert self.sqlexecute is not None
        self.completion_refresher.refresh(
            self.sqlexecute,
            self._on_completions_refreshed,
            {
                "smart_completion": self.smart_completion,
                "supported_formats": self.main_formatter.supported_formats,
                "keyword_casing": self.completer.keyword_casing,
            },
        )

        return [SQLResult(status="Auto-completion refresh started in the background.")]

    def _on_completions_refreshed(self, new_completer: 'SQLCompleter') -> None:
        """Swap the completer object in cli with the newly created completer."""
        with self._completer_lock:
            self.completer = new_completer

        if self.prompt_app:
            # After refreshing, redraw the CLI to clear the statusbar
            # "Refreshing completions..." indicator
            self.prompt_app.app.invalidate()

    def get_completions(self, text: str, cursor_position: int) -> Iterable:
        with self._completer_lock:
            return self.completer.get_completions(Document(text=text, cursor_position=cursor_position), None)

    def get_prompt(self, string: str) -> str:
        sqlexecute = self.sqlexecute
        assert sqlexecute is not None
        assert sqlexecute.server_info is not None
        assert sqlexecute.server_info.species is not None
        if self.login_path and self.login_path_as_host:
            prompt_host = self.login_path
        elif sqlexecute.host is not None:
            prompt_host = sqlexecute.host
        else:
            prompt_host = "localhost"
        now = datetime.now()
        string = string.replace("\\u", sqlexecute.user or "(none)")
        string = string.replace("\\h", prompt_host or "(none)")
        string = string.replace("\\d", sqlexecute.dbname or "(none)")
        string = string.replace("\\t", sqlexecute.server_info.species.name)
        string = string.replace("\\n", "\n")
        string = string.replace("\\D", now.strftime("%a %b %d %H:%M:%S %Y"))
        string = string.replace("\\m", now.strftime("%M"))
        string = string.replace("\\P", now.strftime("%p"))
        string = string.replace("\\R", now.strftime("%H"))
        string = string.replace("\\r", now.strftime("%I"))
        string = string.replace("\\s", now.strftime("%S"))
        string = string.replace("\\p", str(sqlexecute.port))
        string = string.replace("\\A", self.dsn_alias or "(none)")
        string = string.replace("\\_", " ")
        return string

    def run_query(
        self,
        query: str,
        checkpoint: TextIOWrapper | None = None,
        new_line: bool = True,
    ) -> None:
        """Runs *query*."""
        assert self.sqlexecute is not None
        self.log_query(query)
        results = self.sqlexecute.run(query)
        for result in results:
            title = result.title
            cur = result.results
            headers = result.headers
            self.main_formatter.query = query
            self.redirect_formatter.query = query
            output = self.format_output(
                title,
                cur,
                headers,
                special.is_expanded_output(),
                special.is_redirected(),
                self.null_string,
                self.numeric_alignment,
            )
            for line in output:
                self.log_output(line)
                click.echo(line, nl=new_line)

            # get and display warnings if enabled
            if self.show_warnings and isinstance(cur, Cursor) and cur.warning_count > 0:
                warnings = self.sqlexecute.run("SHOW WARNINGS")
                for warning in warnings:
                    title = warning.title
                    cur = warning.results
                    headers = warning.headers
                    output = self.format_output(
                        title,
                        cur,
                        headers,
                        special.is_expanded_output(),
                        special.is_redirected(),
                        self.null_string,
                        self.numeric_alignment,
                    )
                    for line in output:
                        click.echo(line, nl=new_line)
        if checkpoint:
            checkpoint.write(query.rstrip('\n') + '\n')
            checkpoint.flush()

    def format_output(
        self,
        title: str | None,
        cur: Cursor | list[tuple] | None,
        headers: list[str] | str | None,
        expanded: bool = False,
        is_redirected: bool = False,
        null_string: str | None = None,
        numeric_alignment: str = 'right',
        max_width: int | None = None,
    ) -> itertools.chain[str]:
        if is_redirected:
            use_formatter = self.redirect_formatter
        else:
            use_formatter = self.main_formatter

        expanded = expanded or use_formatter.format_name == "vertical"
        output: itertools.chain[str] = itertools.chain()

        output_kwargs = {
            "dialect": "unix",
            "disable_numparse": True,
            "preserve_whitespace": True,
            "style": self.output_style,
        }
        default_kwargs = use_formatter._output_formats[use_formatter.format_name].formatter_args

        if null_string is not None and default_kwargs.get('missing_value') == DEFAULT_MISSING_VALUE:
            output_kwargs['missing_value'] = null_string

        if use_formatter.format_name not in sql_format.supported_formats:
            # will run before preprocessors defined as part of the format in cli_helpers
            output_kwargs["preprocessors"] = (preprocessors.convert_to_undecoded_string,)

        if title:  # Only print the title if it's not None.
            output = itertools.chain(output, [title])

        if headers or (cur and title):
            column_types = None
            colalign = None
            if isinstance(cur, Cursor):

                def get_col_type(col) -> type:
                    col_type = FIELD_TYPES.get(col[1], str)
                    return col_type if type(col_type) is type else str

                if cur.rowcount > 0:
                    column_types = [get_col_type(tup) for tup in cur.description]
                    colalign = [numeric_alignment if x in (int, float, Decimal) else 'left' for x in column_types]
                else:
                    column_types, colalign = [], []

            if max_width is not None and isinstance(cur, Cursor):
                cur = list(cur)

            formatted = use_formatter.format_output(
                cur,
                headers,
                format_name="vertical" if expanded else None,
                column_types=column_types,
                colalign=colalign,
                **output_kwargs,
            )

            if isinstance(formatted, str):
                formatted = formatted.splitlines()
            formatted = iter(formatted)

            if not expanded and max_width and headers and cur:
                first_line = next(formatted)
                if len(strip_ansi(first_line)) > max_width:
                    formatted = use_formatter.format_output(
                        cur,
                        headers,
                        format_name="vertical",
                        column_types=column_types,
                        **output_kwargs,
                    )
                    if isinstance(formatted, str):
                        formatted = iter(formatted.splitlines())
                else:
                    formatted = itertools.chain([first_line], formatted)

            output = itertools.chain(output, formatted)

        return output

    def get_reserved_space(self) -> int:
        """Get the number of lines to reserve for the completion menu."""
        reserved_space_ratio = 0.45
        max_reserved_space = 8
        _, height = shutil.get_terminal_size()
        return min(int(round(height * reserved_space_ratio)), max_reserved_space)

    def get_last_query(self) -> str | None:
        """Get the last query executed or None."""
        return self.query_history[-1][0] if self.query_history else None

    def log_query(self, query: str) -> None:
        if isinstance(self.logfile, TextIOWrapper):
            self.logfile.write(f"\n# {datetime.now()}\n")
            self.logfile.write(query)
            self.logfile.write("\n")

    def log_output(self, output: str) -> None:
        """Log the output in the audit log, if it's enabled."""
        if isinstance(self.logfile, TextIOWrapper):
            click.echo(output, file=self.logfile)
