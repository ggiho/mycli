from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.keys import Keys
import pytest

from mycli import key_bindings, prompt_commands, schema_browser
from test.pytests.test_key_bindings import DummyKeysConfig, DummyMyCli


def test_no_function_keys_are_bound() -> None:
    cli = DummyMyCli(DummyKeysConfig())
    kb = key_bindings.mycli_bindings(cli)
    assert not any(any(str(key).startswith('Keys.F') and str(key)[6:].isdigit() for key in binding.keys) for binding in kb.bindings)
    assert not any(binding.keys[:2] == (Keys.Escape, '[') and binding.keys[-1] in ('P', 'Q', 'R', 'S') for binding in kb.bindings)
    assert {'help', 'smart', 'multiline', 'mode', 'explain'} <= cli.prompt_actions.keys()


def test_browse_returns_draft_without_query(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = SimpleNamespace(sqlexecute=SimpleNamespace(conn=Mock()), syntax_style='native', cli_style={})
    monkeypatch.setattr(schema_browser, 'browse_schema', lambda *a, **kw: ('select', 'SELECT * FROM `db`.`t` LIMIT 100;'))
    assert prompt_commands.run_prompt_command(cli, r'\browse') == (True, 'SELECT * FROM `db`.`t` LIMIT 100;')
    cli.sqlexecute.conn.cursor.assert_not_called()


def test_toggles_use_actions_without_modifying_draft() -> None:
    buffer = Buffer()
    buffer.text = 'SELECT unfinished'
    handler = Mock()
    cli = SimpleNamespace(prompt_actions={'smart': handler}, prompt_session=SimpleNamespace(app=object(), default_buffer=buffer))
    assert prompt_commands.run_prompt_command(cli, r'\smart') == (True, None)
    handler.assert_called_once()
    assert buffer.text == 'SELECT unfinished'
    assert prompt_commands.run_prompt_command(cli, 'SELECT 1') == (False, None)
