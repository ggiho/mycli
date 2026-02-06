"""Connection management mixin for MyCli."""
from __future__ import annotations

import os
import sys
import traceback
from collections import defaultdict
from typing import Any

import click
import keyring
import pymysql
from configobj import ConfigObj
from pymysql.constants.ER import HANDSHAKE_ERROR

from mycli.compat import WIN
from mycli.config import str_to_bool, strip_matching_quotes
from mycli.packages.filepaths import guess_socket_location
from mycli.sqlexecute import SQLExecute

try:
    from pwd import getpwuid
except ImportError:
    pass


class ConnectionMixin:
    """Mixin providing connection management methods for MyCli."""

    def read_my_cnf(self, cnf: ConfigObj, keys: list[str]) -> dict[str, Any]:
        """
        Retrieves some keys from a configuration, applies transformations, returns a new configuration.
        :param cnf: configuration to read
        :param keys: list of keys to retrieve
        :returns: tuple, with None for missing keys.
        """

        sections = ["client", "mysqld"]
        key_transformations = {
            "mysqld": {
                "socket": "default_socket",
                "port": "default_port",
                "user": "default_user",
            },
        }

        if self.login_path and self.login_path != "client":
            sections.append(self.login_path)

        if self.defaults_suffix:
            sections.extend([sect + self.defaults_suffix for sect in sections])

        configuration: dict[str, Any] = defaultdict(lambda: None)
        for key in keys:
            for section in cnf:
                if section not in sections or key not in cnf[section]:
                    continue
                new_key = key_transformations.get(section, {}).get(key) or key
                configuration[new_key] = strip_matching_quotes(cnf[section][key])

        return configuration

    def merge_ssl_with_cnf(self, ssl: dict[str, Any], cnf: dict[str, Any]) -> dict[str, Any]:
        """Merge SSL configuration dict with cnf dict"""

        merged = {}
        merged.update(ssl)
        prefix = "ssl-"
        for k, v in cnf.items():
            # skip unrelated options
            if not k.startswith(prefix):
                continue
            if v is None:
                continue
            # special case because PyMySQL argument is significantly different
            # from commandline
            if k == "ssl-verify-server-cert":
                merged["check_hostname"] = str_to_bool(v)
            else:
                # use argument name just strip "ssl-" prefix
                arg = k[len(prefix):]
                merged[arg] = v

        return merged

    def connect(
        self,
        database: str | None = "",
        user: str | None = "",
        passwd: str | None = None,
        host: str | None = "",
        port: str | int | None = "",
        socket: str | None = "",
        charset: str | None = "",
        local_infile: bool = False,
        ssl: dict[str, Any] | None = None,
        ssh_user: str | None = "",
        ssh_host: str | None = "",
        ssh_port: int = 22,
        ssh_password: str | None = "",
        ssh_key_filename: str | None = "",
        init_command: str | None = "",
        unbuffered: bool | None = None,
        use_keyring: bool | None = None,
        reset_keyring: bool | None = None,
    ) -> None:
        cnf = {
            "database": None,
            "user": None,
            "password": None,
            "host": None,
            "port": None,
            "socket": None,
            "default_socket": None,
            "default-character-set": None,
            "local-infile": None,
            "loose-local-infile": None,
            "ssl-ca": None,
            "ssl-cert": None,
            "ssl-key": None,
            "ssl-cipher": None,
            "ssl-verify-server-cert": None,
        }

        cnf = self.read_my_cnf(self.my_cnf, list(cnf.keys()))

        # Fall back to config values only if user did not specify a value.
        database = database or cnf["database"]
        user = user or cnf["user"] or os.getenv("USER")
        host = host or cnf["host"]
        port = port or cnf["port"]
        ssl_config: dict[str, Any] = ssl or {}

        int_port = port and int(port)
        if not int_port:
            int_port = 3306
            if not host or host == "localhost":
                socket = socket or cnf["socket"] or cnf["default_socket"] or guess_socket_location()

        passwd = passwd if isinstance(passwd, str) else cnf["password"]
        charset = charset or self.config["main"].get("default_character_set") or cnf["default-character-set"] or "utf8mb4"

        # Favor whichever local_infile option is set.
        use_local_infile = False
        for local_infile_option in (local_infile, cnf["local-infile"], cnf["loose-local-infile"], False):
            try:
                use_local_infile = str_to_bool(local_infile_option or '')
                break
            except (TypeError, ValueError):
                pass

        ssl_config_or_none: dict[str, Any] | None = self.merge_ssl_with_cnf(ssl_config, cnf)
        # prune lone check_hostname=False
        if not any(v for v in ssl_config.values()):
            ssl_config_or_none = None

        # password hierarchy
        # 1. -p / --pass/--password CLI options
        # 2. --password-file CLI option
        # 3. envvar (MYSQL_PWD)
        # 4. DSN (mysql://user:password)
        # 5. cnf (.my.cnf / etc)
        # 6. keyring

        keychain_user = f'{user}@{host}'
        keychain_domain = 'mycli.net'
        keychain_retrieved = False

        if passwd is None and use_keyring and not reset_keyring:
            passwd = keyring.get_password(keychain_domain, keychain_user)
            keychain_retrieved = True

        # if no password was found from all of the above sources, ask for a password
        if passwd is None or passwd == "MYCLI_ASK_PASSWORD":
            passwd = click.prompt(f"Enter password for {user}", hide_input=True, show_default=False, default='', type=str, err=True)

        if reset_keyring or (use_keyring and not keychain_retrieved):
            try:
                keyring.set_password(keychain_domain, keychain_user, passwd)
                click.secho('Password saved to the system keychain', err=True)
            except Exception as e:
                click.secho(f'Password not saved to the system keychain: {e}', err=True, fg='red')

        # Connect to the database.
        def _connect() -> None:
            try:
                self.sqlexecute = SQLExecute(
                    database,
                    user,
                    passwd,
                    host,
                    int_port,
                    socket,
                    charset,
                    use_local_infile,
                    ssl_config_or_none,
                    ssh_user,
                    ssh_host,
                    int(ssh_port) if ssh_port else None,
                    ssh_password,
                    ssh_key_filename,
                    init_command,
                    unbuffered,
                )
            except pymysql.OperationalError as e1:
                if e1.args[0] == HANDSHAKE_ERROR and ssl is not None and ssl.get("mode", None) == "auto":
                    try:
                        self.sqlexecute = SQLExecute(
                            database,
                            user,
                            passwd,
                            host,
                            int_port,
                            socket,
                            charset,
                            use_local_infile,
                            None,
                            ssh_user,
                            ssh_host,
                            int(ssh_port) if ssh_port else None,
                            ssh_password,
                            ssh_key_filename,
                            init_command,
                            unbuffered,
                        )
                    except Exception as e2:
                        raise e2
                else:
                    raise e1

        try:
            if not WIN and socket:
                socket_owner = getpwuid(os.stat(socket).st_uid).pw_name
                self.echo(f"Connecting to socket {socket}, owned by user {socket_owner}", err=True)
                try:
                    _connect()
                except pymysql.OperationalError as e:
                    # These are "Can't open socket" and 2x "Can't connect"
                    if [code for code in (2001, 2002, 2003) if code == e.args[0]]:
                        self.logger.debug("Database connection failed: %r.", e)
                        self.logger.error("traceback: %r", traceback.format_exc())
                        self.logger.debug("Retrying over TCP/IP")
                        self.echo(f"Failed to connect to local MySQL server through socket '{socket}':")
                        self.echo(str(e), err=True)
                        self.echo("Retrying over TCP/IP", err=True)

                        # Else fall back to TCP/IP localhost
                        socket = ""
                        host = "localhost"
                        port = 3306
                        _connect()
                    else:
                        raise
            else:
                host = host or "localhost"
                port = port or 3306

                # Bad ports give particularly daft error messages
                try:
                    port = int(port)
                except ValueError:
                    self.echo(f"Error: Invalid port number: '{port}'.", err=True, fg="red")
                    sys.exit(1)

                _connect()
        except Exception as e:  # Connecting to a database could fail.
            self.logger.debug("Database connection failed: %r.", e)
            self.logger.error("traceback: %r", traceback.format_exc())
            self.echo(str(e), err=True, fg="red")
            sys.exit(1)

    def reconnect(self, database: str = "") -> bool:
        """
        Attempt to reconnect to the server. Return True if successful,
        False if unsuccessful.

        The "database" argument is used only to improve messages.
        """
        assert self.sqlexecute is not None
        assert self.sqlexecute.conn is not None

        # First pass with ping(reconnect=False) and minimal feedback levels.  This definitely
        # works as expected, and is a good idea especially when "connect" was used as a
        # synonym for "use".
        try:
            self.sqlexecute.conn.ping(reconnect=False)
            if not database:
                self.echo("Already connected.", fg="yellow")
            return True
        except pymysql.err.Error:
            pass

        # Second pass with ping(reconnect=True).  It is not demonstrated that this pass ever
        # gives the benefit it is looking for, _ie_ preserves session state.  We need to test
        # this with connection pooling.
        try:
            old_connection_id = self.sqlexecute.connection_id
            self.logger.debug("Attempting to reconnect.")
            self.echo("Reconnecting...", fg="yellow")
            self.sqlexecute.conn.ping(reconnect=True)
            # if a database is currently selected, set it on the conn again
            if self.sqlexecute.dbname:
                self.sqlexecute.conn.select_db(self.sqlexecute.dbname)
            self.logger.debug("Reconnected successfully.")
            self.echo("Reconnected successfully.", fg="yellow")
            self.sqlexecute.reset_connection_id()
            if old_connection_id != self.sqlexecute.connection_id:
                self.echo("Any session state was reset.", fg="red")
            return True
        except pymysql.err.Error:
            pass

        # Third pass with sqlexecute.connect() should always work, but always resets session state.
        try:
            self.logger.debug("Creating new connection")
            self.echo("Creating new connection...", fg="yellow")
            self.sqlexecute.connect()
            self.logger.debug("New connection created successfully.")
            self.echo("New connection created successfully.", fg="yellow")
            self.echo("Any session state was reset.", fg="red")
            return True
        except pymysql.OperationalError as e:
            self.logger.debug("Reconnect failed. e: %r", e)
            self.echo(str(e), err=True, fg="red")
            return False
