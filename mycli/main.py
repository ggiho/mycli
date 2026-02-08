from __future__ import annotations

import logging
import os
import re
import threading
from io import TextIOWrapper
from time import sleep
from typing import IO, Any, Generator, Iterable, Literal
from urllib.parse import parse_qs, unquote, urlparse

import click
from cli_helpers.tabular_output import TabularOutputFormatter
from prompt_toolkit.key_binding.bindings.named_commands import register as prompt_register
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.shortcuts import PromptSession
import sys

from mycli import __version__
from mycli.clistyle import style_factory_output
from mycli.compat import WIN
from mycli.completion_refresher import CompletionRefresher
from mycli.config import get_mylogin_cnf_path, open_mylogin_cnf, read_config_files, str_to_bool, write_default_config
from mycli.packages import special
from mycli.packages.filepaths import dir_path_exists
from mycli.packages.parseutils import is_destructive, is_valid_connection_scheme
from mycli.packages.prompt_utils import confirm_destructive_query
from mycli.packages.special.favoritequeries import FavoriteQueries
from mycli.packages.special.main import ArgType
from mycli.packages.sqlresult import SQLResult
from mycli.packages.tabular_output import sql_format
from mycli.sqlcompleter import SQLCompleter
from mycli.sqlexecute import SQLExecute

# Mixin classes for MyCli
from mycli.output_mixin import OutputMixin
from mycli.connection_mixin import ConnectionMixin
from mycli.cliloop_mixin import CLILoopMixin

# Re-export from constants and query_utils for backwards compatibility
from mycli.constants import Query, SUPPORT_INFO, DEFAULT_WIDTH, DEFAULT_HEIGHT
from mycli.query_utils import need_completion_refresh, need_completion_reset, is_mutating, is_select

try:
    import paramiko
except ImportError:
    from mycli.packages.paramiko_stub import paramiko  # type: ignore[no-redef]

import mycli.packages.sqlparse_config  # noqa: F401 — centralized sqlparse settings


class MyCli(OutputMixin, ConnectionMixin, CLILoopMixin):
    default_prompt = "\\t \\u@\\h:\\d> "
    default_prompt_splitln = "\\u@\\h\\n(\\t):\\d>"
    max_len_prompt = 45
    defaults_suffix = None

    # In order of being loaded. Files lower in list override earlier ones.
    cnf_files: list[str | IO[str]] = [
        "/etc/my.cnf",
        "/etc/mysql/my.cnf",
        "/usr/local/etc/my.cnf",
        os.path.expanduser("~/.my.cnf"),
    ]

    # check XDG_CONFIG_HOME exists and not an empty string
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    system_config_files: list[str | IO[str]] = [
        "/etc/myclirc",
        os.path.join(os.path.expanduser(xdg_config_home), "mycli", "myclirc"),
    ]

    pwd_config_file = os.path.join(os.getcwd(), ".myclirc")

    def __init__(
        self,
        sqlexecute: SQLExecute | None = None,
        prompt: str | None = None,
        logfile: TextIOWrapper | Literal[False] | None = None,
        defaults_suffix: str | None = None,
        defaults_file: str | None = None,
        login_path: str | None = None,
        auto_vertical_output: bool = False,
        show_warnings: bool = False,
        warn: bool | None = None,
        myclirc: str = "~/.myclirc",
    ) -> None:
        self.sqlexecute = sqlexecute
        self.logfile = logfile
        self.defaults_suffix = defaults_suffix
        self.login_path = login_path
        self.toolbar_error_message: str | None = None
        self.prompt_app: PromptSession | None = None

        # self.cnf_files is a class variable that stores the list of mysql
        # config files to read in at launch.
        # If defaults_file is specified then override the class variable with
        # defaults_file.
        if defaults_file:
            self.cnf_files = [defaults_file]

        # Load config.
        config_files: list[str | IO[str]] = self.system_config_files + [myclirc] + [self.pwd_config_file]
        c = self.config = read_config_files(config_files)
        self.multi_line = c["main"].as_bool("multi_line")
        self.key_bindings = c["main"]["key_bindings"]
        special.set_timing_enabled(c["main"].as_bool("timing"))
        special.set_show_favorite_query(c["main"].as_bool("show_favorite_query"))
        self.beep_after_seconds = float(c["main"]["beep_after_seconds"] or 0)

        FavoriteQueries.instance = FavoriteQueries.from_config(self.config)

        self.dsn_alias: str | None = None
        self.main_formatter = TabularOutputFormatter(format_name=c["main"]["table_format"])
        self.redirect_formatter = TabularOutputFormatter(format_name=c["main"].get("redirect_format", "csv"))
        sql_format.register_new_formatter(self.main_formatter)
        sql_format.register_new_formatter(self.redirect_formatter)
        self.main_formatter.mycli = self
        self.redirect_formatter.mycli = self
        self.syntax_style = c["main"]["syntax_style"]
        self.less_chatty = c["main"].as_bool("less_chatty")
        self.cli_style = c["colors"]
        self.output_style = style_factory_output(self.syntax_style, self.cli_style)
        self.wider_completion_menu = c["main"].as_bool("wider_completion_menu")
        c_dest_warning = c["main"].as_bool("destructive_warning")
        self.destructive_warning = c_dest_warning if warn is None else warn
        self.login_path_as_host = c["main"].as_bool("login_path_as_host")
        self.post_redirect_command = c['main'].get('post_redirect_command')
        self.null_string = c['main'].get('null_string')
        self.numeric_alignment = c['main'].get('numeric_alignment', 'right')

        # set ssl_mode if a valid option is provided in a config file, otherwise None
        ssl_mode = c["main"].get("ssl_mode", None)
        if ssl_mode not in ("auto", "on", "off", None):
            self.echo(f"Invalid config option provided for ssl_mode ({ssl_mode}); ignoring.", err=True, fg="red")
            self.ssl_mode = None
        else:
            self.ssl_mode = ssl_mode

        # read from cli argument or user config file
        self.auto_vertical_output = auto_vertical_output or c["main"].as_bool("auto_vertical_output")
        self.show_warnings = show_warnings or c["main"].as_bool("show_warnings")

        # Write user config if system config wasn't the last config loaded.
        if c.filename not in self.system_config_files and not os.path.exists(myclirc):
            write_default_config(myclirc)

        # audit log
        if self.logfile is None and "audit_log" in c["main"]:
            try:
                self.logfile = open(os.path.expanduser(c["main"]["audit_log"]), "a")
            except (IOError, OSError):
                self.echo("Error: Unable to open the audit log file. Your queries will not be logged.", err=True, fg="red")
                self.logfile = False

        self.completion_refresher = CompletionRefresher()

        self.logger = logging.getLogger(__name__)
        self.initialize_logging()

        keyword_casing = c["main"].get("keyword_casing", "auto")

        self.query_history: list[Query] = []

        # Initialize completer.
        self.smart_completion = c["main"].as_bool("smart_completion")
        self.completer = SQLCompleter(
            self.smart_completion, supported_formats=self.main_formatter.supported_formats, keyword_casing=keyword_casing
        )
        self._completer_lock = threading.Lock()

        # Register custom special commands.
        self.register_special_commands()

        # Load .mylogin.cnf if it exists.
        mylogin_cnf_path = get_mylogin_cnf_path()
        if mylogin_cnf_path:
            mylogin_cnf = open_mylogin_cnf(mylogin_cnf_path)
            if mylogin_cnf_path and mylogin_cnf:
                # .mylogin.cnf gets read last, even if defaults_file is specified.
                self.cnf_files.append(mylogin_cnf)
            elif mylogin_cnf_path and not mylogin_cnf:
                # There was an error reading the login path file.
                print("Error: Unable to read login path file.")

        self.my_cnf = read_config_files(self.cnf_files, list_values=False)
        prompt_cnf = self.read_my_cnf(self.my_cnf, ["prompt"])["prompt"]
        self.prompt_format = prompt or prompt_cnf or c["main"]["prompt"] or self.default_prompt
        self.multiline_continuation_char = c["main"]["prompt_continuation"]
        self.prompt_app = None
        self.destructive_keywords = [
            keyword for keyword in c["main"].get("destructive_keywords", "DROP SHUTDOWN DELETE TRUNCATE ALTER UPDATE").split(' ') if keyword
        ]
        special.set_destructive_keywords(self.destructive_keywords)

    def close(self) -> None:
        if self.sqlexecute is not None:
            self.sqlexecute.close()

    def register_special_commands(self) -> None:
        special.register_special_command(self.change_db, "use", "\\u", "Change to a new database.", aliases=["\\u"])
        special.register_special_command(
            self.manual_reconnect,
            "connect",
            "\\r",
            "Reconnect to the database. Optional database argument.",
            aliases=["\\r"],
            case_sensitive=True,
        )
        special.register_special_command(
            self.refresh_completions, "rehash", "\\#", "Refresh auto-completions.", arg_type=ArgType.NO_QUERY, aliases=["\\#"]
        )
        special.register_special_command(
            self.change_table_format,
            "tableformat",
            "\\T",
            "Change the table format used to output results.",
            aliases=["\\T"],
            case_sensitive=True,
        )
        special.register_special_command(
            self.change_redirect_format,
            "redirectformat",
            "\\Tr",
            "Change the table format used to output redirected results.",
            aliases=["\\Tr"],
            case_sensitive=True,
        )
        special.register_special_command(
            self.disable_show_warnings,
            "nowarnings",
            "\\w",
            "Disable automatic warnings display.",
            aliases=["\\w"],
            case_sensitive=True,
        )
        special.register_special_command(
            self.enable_show_warnings,
            "warnings",
            "\\W",
            "Enable automatic warnings display.",
            aliases=["\\W"],
            case_sensitive=True,
        )
        special.register_special_command(self.execute_from_file, "source", "\\. filename", "Execute commands from file.", aliases=["\\."])
        special.register_special_command(
            self.change_prompt_format, "prompt", "\\R", "Change prompt format.", aliases=["\\R"], case_sensitive=True
        )

    def manual_reconnect(self, arg: str = "", **_) -> Generator[SQLResult, None, None]:
        """
        Interactive method to use for the \r command, so that the utility method
        may be cleanly used elsewhere.
        """
        if not self.reconnect(database=arg):
            yield SQLResult(status="Not connected")
        elif not arg or arg == '``':
            yield SQLResult()
        else:
            yield self.change_db(arg).send(None)

    def enable_show_warnings(self, **_) -> Generator[SQLResult, None, None]:
        self.show_warnings = True
        msg = "Show warnings enabled."
        yield SQLResult(status=msg)

    def disable_show_warnings(self, **_) -> Generator[SQLResult, None, None]:
        self.show_warnings = False
        msg = "Show warnings disabled."
        yield SQLResult(status=msg)

    def change_table_format(self, arg: str, **_) -> Generator[SQLResult, None, None]:
        try:
            self.main_formatter.format_name = arg
            yield SQLResult(status=f"Changed table format to {arg}")
        except ValueError:
            msg = f"Table format {arg} not recognized. Allowed formats:"
            for table_type in self.main_formatter.supported_formats:
                msg += f"\n\t{table_type}"
            yield SQLResult(status=msg)

    def change_redirect_format(self, arg: str, **_) -> Generator[SQLResult, None, None]:
        try:
            self.redirect_formatter.format_name = arg
            yield SQLResult(status=f"Changed redirect format to {arg}")
        except ValueError:
            msg = f"Redirect format {arg} not recognized. Allowed formats:"
            for table_type in self.redirect_formatter.supported_formats:
                msg += f"\n\t{table_type}"
            yield SQLResult(status=msg)

    def change_db(self, arg: str, **_) -> Generator[SQLResult, None, None]:
        if arg.startswith("`") and arg.endswith("`"):
            arg = re.sub(r"^`(.*)`$", r"\1", arg)
            arg = re.sub(r"``", r"`", arg)

        if not arg:
            click.secho("No database selected", err=True, fg="red")
            return

        if self.sqlexecute is None:
            raise RuntimeError("SQLExecute instance not initialized")

        if self.sqlexecute.dbname == arg:
            msg = f'You are already connected to database "{self.sqlexecute.dbname}" as user "{self.sqlexecute.user}"'
        else:
            self.sqlexecute.change_db(arg)
            msg = f'You are now connected to database "{self.sqlexecute.dbname}" as user "{self.sqlexecute.user}"'

        yield SQLResult(status=msg)

    def execute_from_file(self, arg: str, **_) -> Iterable[SQLResult]:
        if not arg:
            message = "Missing required argument: filename."
            return [SQLResult(status=message)]
        try:
            with open(os.path.expanduser(arg)) as f:
                query = f.read()
        except IOError as e:
            return [SQLResult(status=str(e))]

        if self.destructive_warning and confirm_destructive_query(self.destructive_keywords, query) is False:
            message = "Wise choice. Command execution stopped."
            return [SQLResult(status=message)]

        if self.sqlexecute is None:
            raise RuntimeError("SQLExecute instance not initialized")
        return self.sqlexecute.run(query)

    def change_prompt_format(self, arg: str, **_) -> list[SQLResult]:
        """
        Change the prompt format.
        """
        if not arg:
            message = "Missing required argument, format."
            return [SQLResult(status=message)]

        self.prompt_format = self.get_prompt(arg)
        return [SQLResult(status=f"Changed prompt format to {arg}")]

    def initialize_logging(self) -> None:
        log_file = os.path.expanduser(self.config["main"]["log_file"])
        log_level = self.config["main"]["log_level"]

        level_map = {
            "CRITICAL": logging.CRITICAL,
            "ERROR": logging.ERROR,
            "WARNING": logging.WARNING,
            "INFO": logging.INFO,
            "DEBUG": logging.DEBUG,
        }

        # Disable logging if value is NONE by switching to a no-op handler
        # Set log level to a high value so it doesn't even waste cycles getting called.
        if log_level.upper() == "NONE":
            handler: logging.Handler = logging.NullHandler()
            log_level = "CRITICAL"
        elif dir_path_exists(log_file):
            handler = logging.FileHandler(log_file)
        else:
            self.echo(f'Error: Unable to open the log file "{log_file}".', err=True, fg="red")
            return

        formatter = logging.Formatter("%(asctime)s (%(process)d/%(threadName)s) %(name)s %(levelname)s - %(message)s")

        handler.setFormatter(formatter)

        root_logger = logging.getLogger("mycli")
        root_logger.addHandler(handler)
        root_logger.setLevel(level_map[log_level.upper()])

        logging.captureWarnings(True)

        root_logger.debug("Initializing mycli logging.")
        root_logger.debug("Log file %r.", log_file)

# ---------------------------------------------------------------------------
# Helper functions extracted from cli() for readability
# ---------------------------------------------------------------------------


def _resolve_password(database, password, password_file):
    """Resolve the password from DSN-in-password, password file, or env var.

    Returns (database, password) — database may change if DSN was in password slot.
    """

    def _get_password_from_file(password_file):
        if not password_file:
            return None
        try:
            with open(password_file) as fp:
                password = fp.readline().strip()
                return password
        except FileNotFoundError:
            click.secho(f"Password file '{password_file}' not found", err=True, fg="red")
            sys.exit(1)
        except PermissionError:
            click.secho(f"Permission denied reading password file '{password_file}'", err=True, fg="red")
            sys.exit(1)
        except IsADirectoryError:
            click.secho(f"Path '{password_file}' is a directory, not a file", err=True, fg="red")
            sys.exit(1)
        except Exception as e:
            click.secho(f"Error reading password file '{password_file}': {str(e)}", err=True, fg="red")
            sys.exit(1)

    # if the password value looks like a DSN, treat it as such and
    # prompt for password
    if database is None and password is not None and "://" in password:
        # check if the scheme is valid. We do not actually have any logic for these, but
        # it will most usefully catch the case where we erroneously catch someone's
        # password, and give them an easy error message to follow / report
        is_valid_scheme, scheme = is_valid_connection_scheme(password)
        if not is_valid_scheme:
            click.secho(f"Error: Unknown connection scheme provided for DSN URI ({scheme}://)", err=True, fg="red")
            sys.exit(1)
        database = password
        password = "MYCLI_ASK_PASSWORD"

    # if the password is not specified try to set it using the password_file option
    if password is None and password_file:
        password_from_file = _get_password_from_file(password_file)
        if password_from_file is not None:
            password = password_from_file

    # getting the envvar ourselves because the envvar from a click
    # option cannot be an empty string, but a password can be
    if password is None and os.environ.get("MYSQL_PWD") is not None:
        password = os.environ.get("MYSQL_PWD")

    return database, password


def _validate_batch_format(csv, table, batch_format):
    """Validate and resolve batch_format from --csv, --table, and --format flags.

    Returns the resolved batch_format string.
    """
    if csv and batch_format not in [None, 'csv']:
        click.secho("Conflicting --csv and --format arguments.", err=True, fg="red")
        sys.exit(1)

    if table and batch_format not in [None, 'table']:
        click.secho("Conflicting --table and --format arguments.", err=True, fg="red")
        sys.exit(1)

    if not batch_format:
        batch_format = 'default'

    if csv:
        batch_format = 'csv'

    if table:
        batch_format = 'table'

    return batch_format


def _show_deprecation_warnings(ssl_enable, ssh_user, ssh_host, ssh_password, ssh_key_filename, list_ssh_config, ssh_config_host, ssh_warning_off):
    """Print deprecation warnings for --ssl and SSH options."""
    if ssl_enable is not None:
        click.secho(
            "Warning: The --ssl/--no-ssl CLI options are deprecated and will be removed in a future release. "
            "Please use the ssl_mode config or --ssl-mode CLI options instead.",
            err=True,
            fg="yellow",
        )

    # ssh_port and ssh_config_path have truthy defaults and are not included
    if any([ssh_user, ssh_host, ssh_password, ssh_key_filename, list_ssh_config, ssh_config_host]) and not ssh_warning_off:
        click.secho(
            "Warning: The built-in SSH functionality is soft deprecated and may be removed in a future release. "
            "Please discuss or vote on this at https://github.com/dbcli/mycli/issues/1464",
            err=True,
            fg="red",
        )


def _list_dsn_and_exit(mycli, verbose):
    """List configured DSN aliases and exit."""
    try:
        alias_dsn = mycli.config["alias_dsn"]
    except KeyError:
        click.secho("Invalid DSNs found in the config file. Please check the \"[alias_dsn]\" section in myclirc.", err=True, fg="red")
        sys.exit(1)
    except Exception as e:
        click.secho(str(e), err=True, fg="red")
        sys.exit(1)
    for alias, value in alias_dsn.items():
        if verbose:
            click.secho(f"{alias} : {value}")
        else:
            click.secho(alias)
    sys.exit(0)


def _list_ssh_config_and_exit(ssh_config_path, verbose):
    """List SSH config hosts and exit."""
    ssh_config = read_ssh_config(ssh_config_path)
    try:
        host_entries = ssh_config.get_hostnames()
    except KeyError:
        click.secho('Error reading ssh config', err=True, fg="red")
        sys.exit(1)
    for host_entry in host_entries:
        if verbose:
            host_config = ssh_config.lookup(host_entry)
            click.secho(f"{host_entry} : {host_config.get('hostname')}")
        else:
            click.secho(host_entry)
    sys.exit(0)


def _resolve_dsn(database, dbname, dsn, mycli, user=None, password=None, host=None, port=None, login_path=None):
    """Resolve DSN alias, URI, and database name.

    Returns (database, dsn, dsn_uri).
    """
    # Choose which ever one has a valid value.
    database = dbname or database

    dsn_uri = None

    # Treat the database argument as a DSN alias only if it matches a configured alias
    if (
        database
        and "://" not in database
        and not any([user, password, host, port, login_path])
        and database in mycli.config.get("alias_dsn", {})
    ):
        dsn, database = database, ""

    if database and "://" in database:
        dsn_uri, database = database, ""

    if dsn:
        try:
            dsn_uri = mycli.config["alias_dsn"][dsn]
        except KeyError:
            click.secho(
                "Could not find the specified DSN in the config file. Please check the \"[alias_dsn]\" section in your myclirc.",
                err=True,
                fg="red",
            )
            sys.exit(1)
        else:
            mycli.dsn_alias = dsn

    return database, dsn, dsn_uri


def _build_ssl_config(ssl_mode, ssl_enable, ssl_ca, ssl_cert, ssl_key, ssl_capath, ssl_cipher, tls_version, ssl_verify_server_cert):
    """Build the SSL config dict (or None) from the resolved SSL parameters."""
    if ssl_mode in ("auto", "on") or (ssl_enable and ssl_mode is None):
        ssl = {
            "mode": ssl_mode,
            "enable": ssl_enable,
            "ca": ssl_ca and os.path.expanduser(ssl_ca),
            "cert": ssl_cert and os.path.expanduser(ssl_cert),
            "key": ssl_key and os.path.expanduser(ssl_key),
            "capath": ssl_capath,
            "cipher": ssl_cipher,
            "tls_version": tls_version,
            "check_hostname": ssl_verify_server_cert,
        }
        # remove empty ssl options
        ssl = {k: v for k, v in ssl.items() if v is not None}
    else:
        ssl = None
    return ssl


def _merge_init_commands(mycli_config, dsn, init_command):
    """Merge init-commands from global config, DSN-specific config, and CLI option.

    Returns the combined init command string.
    """
    init_cmds: list[str] = []
    # 1) Global init-commands
    global_section = mycli_config.get("init-commands", {})
    for _, val in global_section.items():
        if isinstance(val, (list, tuple)):
            init_cmds.extend(val)
        elif val:
            init_cmds.append(val)
    # 2) DSN-specific init-commands
    if dsn:
        alias_section = mycli_config.get("alias_dsn.init-commands", {})
        if dsn in alias_section:
            val = alias_section.get(dsn)
            if isinstance(val, (list, tuple)):
                init_cmds.extend(val)
            elif val:
                init_cmds.append(val)
    # 3) CLI-provided init_command
    if init_command:
        init_cmds.append(init_command)

    return "; ".join(cmd.strip() for cmd in init_cmds if cmd)


def _resolve_batch_format_name(batch_format, is_first):
    """Resolve the formatter name for batch/execute mode."""
    if batch_format == 'csv':
        return 'csv' if is_first else 'csv-noheader'
    elif batch_format == 'tsv':
        return 'tsv' if is_first else 'tsv_noheader'
    elif batch_format == 'table':
        return 'ascii'
    else:
        return 'tsv'


def _run_stdin_pipe(mycli, batch_format, noninteractive, throttle, checkpoint):
    """Handle piped stdin input in batch mode."""
    stdin = click.get_text_stream("stdin")
    counter = 0
    for stdin_text in stdin:
        is_first = counter == 0
        mycli.main_formatter.format_name = _resolve_batch_format_name(batch_format, is_first)
        counter += 1
        warn_confirmed: bool | None = True
        if not noninteractive and mycli.destructive_warning and is_destructive(mycli.destructive_keywords, stdin_text):
            try:
                # this seems to work, even though we are reading from stdin above
                sys.stdin = open("/dev/tty")
                # bug: the prompt will not be visible if stdout is redirected
                warn_confirmed = confirm_destructive_query(mycli.destructive_keywords, stdin_text)
            except (IOError, OSError):
                mycli.logger.warning("Unable to open TTY as stdin.")
                sys.exit(1)
        try:
            if warn_confirmed:
                if throttle and counter > 1:
                    sleep(throttle)
                mycli.run_query(stdin_text, checkpoint=checkpoint, new_line=True)
        except Exception as e:
            click.secho(str(e), err=True, fg="red")
            sys.exit(1)
    sys.exit(0)


@click.command()
@click.option("-h", "--host", envvar="MYSQL_HOST", help="Host address of the database.")
@click.option("-P", "--port", envvar="MYSQL_TCP_PORT", type=int, help="Port number to use for connection. Honors $MYSQL_TCP_PORT.")
@click.option("-u", "--user", help="User name to connect to the database.")
@click.option("-S", "--socket", envvar="MYSQL_UNIX_PORT", help="The socket file to use for connection.")
@click.option(
    "-p",
    "--pass",
    "--password",
    "password",
    is_flag=False,
    flag_value="MYCLI_ASK_PASSWORD",
    type=str,
    help="Prompt for (or enter in cleartext) password to connect to the database.",
)
@click.option("--ssh-user", help="User name to connect to ssh server.")
@click.option("--ssh-host", help="Host name to connect to ssh server.")
@click.option("--ssh-port", default=22, help="Port to connect to ssh server.")
@click.option("--ssh-password", help="Password to connect to ssh server.")
@click.option("--ssh-key-filename", help="Private key filename (identify file) for the ssh connection.")
@click.option("--ssh-config-path", help="Path to ssh configuration.", default=os.path.expanduser("~") + "/.ssh/config")
@click.option("--ssh-config-host", help="Host to connect to ssh server reading from ssh configuration.")
@click.option(
    "--ssl-mode",
    "ssl_mode",
    help="Set desired SSL behavior. auto=preferred, on=required, off=off.",
    type=click.Choice(["auto", "on", "off"]),
)
@click.option("--ssl/--no-ssl", "ssl_enable", default=None, help="Enable SSL for connection (automatically enabled with other flags).")
@click.option("--ssl-ca", help="CA file in PEM format.", type=click.Path(exists=True))
@click.option("--ssl-capath", help="CA directory.")
@click.option("--ssl-cert", help="X509 cert in PEM format.", type=click.Path(exists=True))
@click.option("--ssl-key", help="X509 key in PEM format.", type=click.Path(exists=True))
@click.option("--ssl-cipher", help="SSL cipher to use.")
@click.option(
    "--tls-version",
    type=click.Choice(["TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3"], case_sensitive=False),
    help="TLS protocol version for secure connection.",
)
@click.option(
    "--ssl-verify-server-cert",
    is_flag=True,
    help=("""Verify server's "Common Name" in its cert against hostname used when connecting. This option is disabled by default."""),
)
@click.version_option(__version__, "-V", "--version", help="Output mycli's version.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.option("-D", "--database", "dbname", help="Database to use.")
@click.option("-d", "--dsn", default="", envvar="DSN", help="Use DSN configured into the [alias_dsn] section of myclirc file.")
@click.option("--list-dsn", "list_dsn", is_flag=True, help="list of DSN configured into the [alias_dsn] section of myclirc file.")
@click.option("--list-ssh-config", "list_ssh_config", is_flag=True, help="list ssh configurations in the ssh config (requires paramiko).")
@click.option("--ssh-warning-off", is_flag=True, help="Suppress the SSH deprecation notice.")
@click.option("-R", "--prompt", "prompt", help=f'Prompt format (Default: "{MyCli.default_prompt}").')
@click.option("-l", "--logfile", type=click.File(mode="a", encoding="utf-8"), help="Log every query and its results to a file.")
@click.option(
    "--checkpoint", type=click.File(mode="a", encoding="utf-8"), help="In batch or --execute mode, log successful queries to a file."
)
@click.option("--defaults-group-suffix", type=str, help="Read MySQL config groups with the specified suffix.")
@click.option("--defaults-file", type=click.Path(), help="Only read MySQL options from the given file.")
@click.option("--myclirc", type=click.Path(), default="~/.myclirc", help="Location of myclirc file.")
@click.option(
    "--auto-vertical-output",
    is_flag=True,
    help="Automatically switch to vertical output mode if the result is wider than the terminal width.",
)
@click.option(
    "--show-warnings/--no-show-warnings", "show_warnings", is_flag=True, help="Automatically show warnings after executing a SQL statement."
)
@click.option("-t", "--table", is_flag=True, help="Shorthand for --format=table.")
@click.option("--csv", is_flag=True, help="Shorthand for --format=csv.")
@click.option("--warn/--no-warn", default=None, help="Warn before running a destructive query.")
@click.option("--local-infile", type=bool, help="Enable/disable LOAD DATA LOCAL INFILE.")
@click.option("-g", "--login-path", type=str, help="Read this path from the login file.")
@click.option("-e", "--execute", type=str, help="Execute command and quit.")
@click.option("--init-command", type=str, help="SQL statement to execute after connecting.")
@click.option(
    "--unbuffered", is_flag=True, help="Instead of copying every row of data into a buffer, fetch rows as needed, to save memory."
)
@click.option("--charset", type=str, help="Character set for MySQL session.")
@click.option(
    "--password-file", type=click.Path(), help="File or FIFO path containing the password to connect to the db if not specified otherwise."
)
@click.argument("database", default=None, nargs=1)
@click.option("--noninteractive", is_flag=True, help="Don't prompt during batch input.  Recommended.")
@click.option(
    '--format', 'batch_format', type=click.Choice(['default', 'csv', 'tsv', 'table']), help='Format for batch or --execute output.'
)
@click.option('--throttle', type=float, default=0.0, help='Pause in seconds between queries in batch mode.')
@click.option(
    '--use-keyring',
    'use_keyring_cli_opt',
    type=click.Choice(['true', 'false', 'reset']),
    default=None,
    help='Store and retrieve passwords from the system keyring: true/false/reset.',
)
@click.pass_context
def cli(
    ctx: click.Context,
    database: str | None,
    user: str | None,
    host: str | None,
    port: int | None,
    socket: str | None,
    password: str | None,
    dbname: str | None,
    verbose: bool,
    prompt: str | None,
    logfile: TextIOWrapper | None,
    checkpoint: TextIOWrapper | None,
    defaults_group_suffix: str | None,
    defaults_file: str | None,
    login_path: str | None,
    auto_vertical_output: bool,
    show_warnings: bool,
    local_infile: bool,
    ssl_mode: str | None,
    ssl_enable: bool,
    ssl_ca: str | None,
    ssl_capath: str | None,
    ssl_cert: str | None,
    ssl_key: str | None,
    ssl_cipher: str | None,
    tls_version: str | None,
    ssl_verify_server_cert: bool,
    table: bool,
    csv: bool,
    warn: bool | None,
    execute: str | None,
    myclirc: str,
    dsn: str,
    list_dsn: str | None,
    ssh_user: str | None,
    ssh_host: str | None,
    ssh_port: int,
    ssh_password: str | None,
    ssh_key_filename: str | None,
    list_ssh_config: bool,
    ssh_config_path: str,
    ssh_config_host: str | None,
    ssh_warning_off: bool | None,
    init_command: str | None,
    unbuffered: bool | None,
    charset: str | None,
    password_file: str | None,
    noninteractive: bool,
    batch_format: str | None,
    throttle: float,
    use_keyring_cli_opt: str | None,
) -> None:
    """A MySQL terminal client with auto-completion and syntax highlighting.

    \b
    Examples:
      - mycli my_database
      - mycli -u my_user -h my_host.com my_database
      - mycli mysql://my_user@my_host.com:3306/my_database

    """

    database, password = _resolve_password(database, password, password_file)

    mycli = MyCli(
        prompt=prompt,
        logfile=logfile,
        defaults_suffix=defaults_group_suffix,
        defaults_file=defaults_file,
        login_path=login_path,
        auto_vertical_output=auto_vertical_output,
        warn=warn,
        myclirc=myclirc,
    )

    batch_format = _validate_batch_format(csv, table, batch_format)

    _show_deprecation_warnings(ssl_enable, ssh_user, ssh_host, ssh_password, ssh_key_filename, list_ssh_config, ssh_config_host, ssh_warning_off)

    if list_dsn:
        _list_dsn_and_exit(mycli, verbose)
    if list_ssh_config:
        _list_ssh_config_and_exit(ssh_config_path, verbose)
    database, dsn, dsn_uri = _resolve_dsn(database, dbname, dsn, mycli, user=user, password=password, host=host, port=port, login_path=login_path)

    if dsn_uri:
        uri = urlparse(dsn_uri)
        if not database:
            database = uri.path[1:]  # ignore the leading fwd slash
        if not user and uri.username is not None:
            user = unquote(uri.username)
        if not password and uri.password is not None:
            password = unquote(uri.password)
        if not host:
            host = uri.hostname
        if not port:
            port = uri.port

        if uri.query:
            dsn_params = parse_qs(uri.query)
        else:
            dsn_params = {}

        if params := dsn_params.get('ssl'):
            ssl_enable = ssl_enable or (params[0].lower() == 'true')
        if params := dsn_params.get('ssl_ca'):
            ssl_ca = ssl_ca or params[0]
            ssl_enable = True
        if params := dsn_params.get('ssl_capath'):
            ssl_capath = ssl_capath or params[0]
            ssl_enable = True
        if params := dsn_params.get('ssl_cert'):
            ssl_cert = ssl_cert or params[0]
            ssl_enable = True
        if params := dsn_params.get('ssl_key'):
            ssl_key = ssl_key or params[0]
            ssl_enable = True
        if params := dsn_params.get('ssl_cipher'):
            ssl_cipher = ssl_cipher or params[0]
            ssl_enable = True
        if params := dsn_params.get('tls_version'):
            tls_version = tls_version or params[0]
            ssl_enable = True
        if params := dsn_params.get('ssl_verify_server_cert'):
            ssl_verify_server_cert = ssl_verify_server_cert or (params[0].lower() == 'true')
            ssl_enable = True

    ssl_mode = ssl_mode or mycli.ssl_mode  # cli option or config option

    # if there is a mismatch between the ssl_mode value and other sources of ssl config, show a warning
    # specifically using "is False" to not pickup the case where ssl_enable is None (not set by the user)
    if ssl_enable and ssl_mode == "off" or ssl_enable is False and ssl_mode in ("auto", "on"):
        click.secho(
            f"Warning: The current ssl_mode value of '{ssl_mode}' is overriding the value provided by "
            f"either the --ssl/--no-ssl CLI options or a DSN URI parameter (ssl={ssl_enable}).",
            err=True,
            fg="yellow",
        )

    ssl = _build_ssl_config(ssl_mode, ssl_enable, ssl_ca, ssl_cert, ssl_key, ssl_capath, ssl_cipher, tls_version, ssl_verify_server_cert)

    if ssh_config_host:
        ssh_config = read_ssh_config(ssh_config_path).lookup(ssh_config_host)
        ssh_host = ssh_host if ssh_host else ssh_config.get("hostname")
        ssh_user = ssh_user if ssh_user else ssh_config.get("user")
        if ssh_config.get("port") and ssh_port == 22:
            # port has a default value, overwrite it if it's in the config
            ssh_port = int(ssh_config.get("port"))
        ssh_key_filename = ssh_key_filename if ssh_key_filename else ssh_config.get("identityfile", [None])[0]

    ssh_key_filename = ssh_key_filename and os.path.expanduser(ssh_key_filename)
    combined_init_cmd = _merge_init_commands(mycli.config, dsn, init_command)

    # --show-warnings / --no-show-warnings
    if show_warnings:
        mycli.show_warnings = show_warnings

    if use_keyring_cli_opt is not None and use_keyring_cli_opt.lower() == 'reset':
        use_keyring = True
        reset_keyring = True
    elif use_keyring_cli_opt is None:
        use_keyring = str_to_bool(mycli.config['main'].get('use_keyring', 'False'))
        reset_keyring = False
    else:
        use_keyring = str_to_bool(use_keyring_cli_opt)
        reset_keyring = False

    mycli.connect(
        database=database,
        user=user,
        passwd=password,
        host=host,
        port=port,
        socket=socket,
        local_infile=local_infile,
        ssl=ssl,
        ssh_user=ssh_user,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_password=ssh_password,
        ssh_key_filename=ssh_key_filename,
        init_command=combined_init_cmd,
        unbuffered=unbuffered,
        charset=charset,
        use_keyring=use_keyring,
        reset_keyring=reset_keyring,
    )

    if combined_init_cmd:
        click.echo(f"Executing init-command: {combined_init_cmd}", err=True)

    mycli.logger.debug("Launch Params: \n\tdatabase: %r\tuser: %r\thost: %r\tport: %r", database, user, host, port)

    #  --execute argument
    if execute:
        try:
            mycli.main_formatter.format_name = _resolve_batch_format_name(batch_format, is_first=True)
            if execute.endswith(r'\G') and batch_format in ('csv', 'tsv', 'table'):
                execute = execute[:-2]

            mycli.run_query(execute, checkpoint=checkpoint)
            sys.exit(0)
        except Exception as e:
            click.secho(str(e), err=True, fg="red")
            sys.exit(1)

    if sys.stdin.isatty():
        mycli.run_cli()
    else:
        _run_stdin_pipe(mycli, batch_format, noninteractive, throttle, checkpoint)
    mycli.close()


def need_completion_refresh(queries: str) -> bool:
    """Determines if the completion needs a refresh by checking if the sql
    statement is an alter, create, drop or change db."""
    for query in sqlparse.split(queries):
        try:
            first_token = query.split()[0]
            if first_token.lower() in ("alter", "create", "use", "\\r", "\\u", "connect", "drop", "rename"):
                return True
        except Exception:
            return False
    return False


def need_completion_reset(queries: str) -> bool:
    """Determines if the statement is a database switch such as 'use' or '\\u'.
    When a database is changed the existing completions must be reset before we
    start the completion refresh for the new database.
    """
    for query in sqlparse.split(queries):
        try:
            first_token = query.split()[0]
            if first_token.lower() in ("use", "\\u"):
                return True
        except Exception:
            return False
    return False


def is_mutating(status: str | None) -> bool:
    """Determines if the statement is mutating based on the status."""
    if not status:
        return False

    mutating = {"insert", "update", "delete", "alter", "create", "drop", "replace", "truncate", "load", "rename"}
    return status.split(None, 1)[0].lower() in mutating


def is_select(status: str | None) -> bool:
    """Returns true if the first word in status is 'select'."""
    if not status:
        return False
    return status.split(None, 1)[0].lower() == "select"


def thanks_picker() -> str:
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


@prompt_register("edit-and-execute-command")
def edit_and_execute(event: KeyPressEvent) -> None:
    """Different from the prompt-toolkit default, we want to have a choice not
    to execute a query after editing, hence validate_and_handle=False."""
    buff = event.current_buffer
    buff.open_in_editor(validate_and_handle=False)


def read_ssh_config(ssh_config_path: str):
    ssh_config = paramiko.config.SSHConfig()
    try:
        with open(ssh_config_path) as f:
            ssh_config.parse(f)
    except FileNotFoundError as e:
        click.secho(str(e), err=True, fg="red")
        sys.exit(1)
    # Paramiko prior to version 2.7 raises Exception on parse errors.
    # In 2.7 it has become paramiko.ssh_exception.SSHException,
    # but let's catch everything for compatibility
    except Exception as err:
        click.secho(f"Could not parse SSH configuration file {ssh_config_path}:\n{err} ", err=True, fg="red")
        sys.exit(1)
    else:
        return ssh_config


if __name__ == "__main__":
    cli()
