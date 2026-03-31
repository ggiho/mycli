from typing import Callable

from prompt_toolkit.application import get_app
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.key_binding.vi_state import InputMode

from mycli.packages import special


def _build_connection_info(mycli) -> str:
    """Build a compact connection info string: user@host:port/db"""
    sqle = mycli.sqlexecute
    if sqle is None:
        return ""

    parts = []
    user = sqle.user or ""
    host = sqle.host or "localhost"
    port = sqle.port
    db = sqle.dbname or "(none)"

    if user:
        parts.append(f"{user}@{host}")
    else:
        parts.append(host)

    if port and port != 3306:
        parts.append(f":{port}")

    parts.append(f"/{db}")
    return "".join(parts)


def _build_server_info(mycli) -> str:
    """Build a compact server info string: e.g. 'MySQL 8.0.35'"""
    sqle = mycli.sqlexecute
    if sqle is None or sqle.server_info is None:
        return ""
    return str(sqle.server_info)


def _build_ssl_indicator(mycli) -> str:
    """Return 'SSL' if the connection uses SSL/TLS."""
    sqle = mycli.sqlexecute
    if sqle is None or not sqle.ssl:
        return ""
    return "SSL"


def create_toolbar_tokens_func(mycli, show_fish_help: Callable) -> Callable:
    """Return a function that generates the toolbar tokens."""

    def get_toolbar_tokens() -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []

        # Left section: connection info
        conn_info = _build_connection_info(mycli)
        if conn_info:
            result.append(("class:bottom-toolbar", f" {conn_info} "))

        server_info = _build_server_info(mycli)
        if server_info:
            result.append(("class:bottom-toolbar.on", f" {server_info} "))

        ssl_indicator = _build_ssl_indicator(mycli)
        if ssl_indicator:
            result.append(("class:bottom-toolbar.transaction.valid", f" {ssl_indicator} "))

        # Separator
        if result:
            result.append(("class:bottom-toolbar", " │"))

        # Center section: mode indicators
        if mycli.multi_line:
            delimiter = special.get_current_delimiter()
            result.append((
                "class:bottom-toolbar",
                f' ({";" if delimiter == ";" else delimiter} ends line) ',
            ))

        if mycli.multi_line:
            result.append(("class:bottom-toolbar.on", "[F3] Multi:ON "))
        else:
            result.append(("class:bottom-toolbar.off", " [F3] Multi:OFF "))

        if mycli.prompt_app and mycli.prompt_app.editing_mode == EditingMode.VI:
            result.append(("class:bottom-toolbar.on", f" Vi({_get_vi_mode()})"))

        # Right section: transient messages
        if mycli.toolbar_error_message:
            result.append(("class:bottom-toolbar.transaction.failed", f"  {mycli.toolbar_error_message}"))
            mycli.toolbar_error_message = None

        if show_fish_help():
            result.append(("class:bottom-toolbar", "  → to complete"))

        if mycli.completion_refresher.is_refreshing():
            current = mycli.completion_refresher.current_refresher or 'completions'
            result.append(("class:bottom-toolbar", f"  ⟳ {current}"))

        return result

    return get_toolbar_tokens


def _get_vi_mode() -> str:
    """Get the current vi mode for display."""
    return {
        InputMode.INSERT: "I",
        InputMode.NAVIGATION: "N",
        InputMode.REPLACE: "R",
        InputMode.REPLACE_SINGLE: "R",
        InputMode.INSERT_MULTIPLE: "M",
    }[get_app().vi_state.input_mode]
