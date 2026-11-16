"""Function-key actions exposed as typed interactive commands."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import click

HELP = r'''\browse       Search tables and inspect columns/indexes
\smart        Toggle smart completion
\multiline    Toggle multiline input
\mode         Toggle Vi/Emacs editing
\explain SQL  Preview a statement's EXPLAIN plan
\keys         Show this list (no function keys required)
\help         CLI help'''


def run_prompt_command(cli: Any, text: str) -> tuple[bool, str | None]:
    parts = text.strip().rstrip(';').split(maxsplit=1)
    if not parts or parts[0] not in (r'\browse', r'\smart', r'\multiline', r'\mode', r'\explain', r'\keys'):
        return False, None
    command = parts[0][1:]
    argument = parts[1] if len(parts) == 2 else ''
    if command != 'explain' and argument:
        click.echo(f'Usage: {parts[0]}')
        return True, None
    if command == 'keys':
        click.echo(HELP)
    elif command == 'browse':
        from mycli.clistyle import style_factory_ptoolkit
        from mycli.schema_browser import browse_schema
        from mycli.schema_metadata import SchemaMetadata

        result = browse_schema(SchemaMetadata(cli.sqlexecute.conn), style=style_factory_ptoolkit(cli.syntax_style, cli.cli_style))
        if result:
            return True, result[1]
    elif command == 'explain':
        from mycli.key_bindings import EXPLAIN_RUNNING, _cancel_explain, _run_explain, explainable_statement

        sql = explainable_statement(argument, len(argument))
        if sql is None:
            click.echo(r'Usage: \explain SELECT ...')
        else:
            EXPLAIN_RUNNING.set()
            try:
                _run_explain(cli, sql)
            except KeyboardInterrupt:
                _cancel_explain(cli.sqlexecute)
            finally:
                EXPLAIN_RUNNING.clear()
    else:
        action = cli.prompt_actions[command]
        action(SimpleNamespace(app=cli.prompt_session.app, current_buffer=cli.prompt_session.default_buffer))
        click.echo(f'{command}: updated')
    return True, None
