"""Exercise the real REPL-to-browser terminal handoff without a database."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX terminal integration')

HARNESS = r'''
import json,sys
from types import SimpleNamespace
from unittest.mock import Mock
from configobj import ConfigObj
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from pytest import MonkeyPatch
from test.pytests.test_main_modes_repl import make_repl_cli,patch_repl_runtime_defaults
from mycli.main_modes import repl
from mycli.packages.special.favoritequeries import FavoriteQueries
config=ConfigObj(encoding='utf-8');config.filename=sys.argv[1]
config['favorite_queries']={
 'orders': 'SELECT * FROM orders',
 'template': "SELECT $1, '{{ kv.name }}'",
 'vars': "SHOW GLOBAL VARIABLES LIKE '%{{ kv.n }}%'",
};config.write()
FavoriteQueries.instance=FavoriteQueries(config,sys.argv[1])
patch=MonkeyPatch();patch_repl_runtime_defaults(patch)
patch.setattr(repl,'render_prompt_string',lambda *args:ANSI('qa> '))
executor=SimpleNamespace(conn=Mock(),run=Mock(side_effect=AssertionError('unexpected SQL execution')),dbname='qa')
cli=make_repl_cli(executor);cli.prompt_session=PromptSession();cli.register_favorite_commands=Mock()
cli._favorite_variable_names=SimpleNamespace(get=lambda _connection:['connect_timeout','max_connections','wait_timeout'])
state=repl.ReplState()
repl._one_iteration(cli,state)
print('RESULT_JSON '+json.dumps({'buffer':state.buffer_text,'queries':dict(ConfigObj(sys.argv[1])['favorite_queries']),
 'sql_calls':executor.run.call_count,'cursor_calls':executor.conn.cursor.call_count,'refreshes':cli.register_favorite_commands.call_count}))
'''


@pytest.mark.parametrize('case', ['template', 'vars', 'vars_pick', 'edit'])
def test_terminal_load_and_save_restore_screen_without_sql(tmp_path: Path, case: str) -> None:
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    initial = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    editing = case == 'edit'
    config = tmp_path / (case + '-config')
    env = dict(os.environ, PROMPT_TOOLKIT_NO_CPR='1', TERM='xterm-256color')
    child = subprocess.Popen(
        [sys.executable, '-c', HARNESS, str(config)], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
    )
    output = bytearray()

    def until(marker: bytes) -> None:
        deadline = time.monotonic() + 20
        while marker not in output:
            if time.monotonic() > deadline:
                pytest.fail(f'timed out waiting for {marker!r}: {bytes(output)[-4000:]!r}')
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b''
                if chunk:
                    output.extend(chunk)
                elif child.poll() is not None:
                    pytest.fail(f'child exited before {marker!r}: {bytes(output)[-4000:]!r}')

    try:
        until(b'qa>')
        if editing:
            os.write(master, b'\\fe orders\r')
            until(b'Edit favorite')
            os.write(master, b'\x01\x0bSELECT 23\t\r')
        else:
            os.write(master, b'\\f\r')
            until(b'SQL preview')
            os.write(master, (b'vars' if case == 'vars_pick' else case.encode()) + b'\r')
            until(b'Parameters')
            if case == 'vars_pick':
                until(b'connect_timeout')
                os.write(master, b'timeout\x1b[B\r')
            else:
                os.write(master, b'timeout\r' if case == 'vars' else b'23\rKim\r')
        until(b'RESULT_JSON ')
        deadline = time.monotonic() + 5
        while child.poll() is None:
            assert time.monotonic() < deadline, bytes(output)[-4000:]
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
            if len(output) > 100000:
                pytest.fail('unexpected terminal output growth')
        assert child.wait(timeout=3) == 0, bytes(output)[-4000:]
        # Drain the last write, which can follow the marker in another chunk.
        while select.select([master], [], [], 0.05)[0]:
            output.extend(os.read(master, 65536))
        payload = bytes(output).split(b'RESULT_JSON ', 1)[1].splitlines()[0]
        result = json.loads(payload)
        assert result['sql_calls'] == result['cursor_calls'] == 0
        if editing:
            assert result['buffer'] is None and result['queries']['orders'] == 'SELECT 23'
            assert result['refreshes'] == 1
        else:
            expected = (
                "SHOW GLOBAL VARIABLES LIKE '%connect_timeout%';"
                if case == 'vars_pick'
                else "SHOW GLOBAL VARIABLES LIKE '%timeout%';"
                if case == 'vars'
                else "SELECT 23, 'Kim';"
            )
            assert result['buffer'] == expected and result['refreshes'] == 0
            assert result['queries']['template'] == "SELECT $1, '{{ kv.name }}'"
            assert result['queries']['orders'] == 'SELECT * FROM orders'
        assert b'\x1b[?1049h' in output and b'\x1b[?1049l' in output
        restored = termios.tcgetattr(slave)
        # BSD may mark cooked input pending for retyping while restoring it.
        restored[3] &= ~getattr(termios, 'PENDIN', 0)
        initial[3] &= ~getattr(termios, 'PENDIN', 0)
        assert restored == initial
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        os.close(master)
        os.close(slave)


ROUNDTRIP_HARNESS = (
    HARNESS.split('repl._one_iteration(cli,state)', 1)[0]
    + r'''
from mycli import favorite_browser
captured={}
def capture_browser(favorites, **kwargs):
    browser=favorite_browser.FavoriteBrowser(favorites, **kwargs)
    result=browser.application.run()
    captured['search']=browser.search.text
    captured['cursor']=browser.search.buffer.cursor_position
    return result
patch.setattr(favorite_browser,'browse_favorites',capture_browser)
repl._one_iteration(cli,state)
print('BROWSER_JSON '+json.dumps(captured),flush=True)
text=cli.prompt_session.prompt(message='sql> ',default='SELECT 12345')
print('EDIT_JSON '+json.dumps({'text':text,'sql_calls':executor.run.call_count}),flush=True)
'''
)


def test_escape_backspace_and_query_prompt_roundtrip(tmp_path: Path) -> None:
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    env = dict(os.environ, PROMPT_TOOLKIT_NO_CPR='1', TERM='xterm-256color')
    child = subprocess.Popen(
        [sys.executable, '-c', ROUNDTRIP_HARNESS, str(tmp_path / 'config')],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    output = bytearray()

    def until(marker: bytes, offset: int = 0, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while marker not in output[offset:]:
            assert time.monotonic() < deadline, bytes(output)[-4000:]
            if select.select([master], [], [], 0.01)[0]:
                chunk = os.read(master, 65536)
                if not chunk and child.poll() is not None:
                    pytest.fail(f'child ended before {marker!r}: {bytes(output)[-4000:]!r}')
                output.extend(chunk)

    try:
        until(b'qa>')
        os.write(master, b'\\f\r')
        until(b'SQL preview')
        os.write(master, b'vars\rtimeout')
        until(b'Parameters')
        offset = len(output)
        os.write(master, b'\x1b')
        until(b'Search:', offset, 0.4)
        os.write(master, b'\x7f\x7f\x7f\x7forders')
        offset = len(output)
        os.write(master, b'\x1b')
        until(b'hjkl move', offset, 0.4)
        os.write(master, b'\x1b')
        until(b'BROWSER_JSON ')
        until(b'sql>')
        os.write(master, b'\x7f\x08\x1b[D\x1b[3~X\r')
        until(b'EDIT_JSON ')
        child.wait(timeout=5)
        while select.select([master], [], [], 0.05)[0]:
            output.extend(os.read(master, 65536))
        browser = json.loads(bytes(output).split(b'BROWSER_JSON ', 1)[1].splitlines()[0])
        result = json.loads(bytes(output).split(b'EDIT_JSON ', 1)[1].splitlines()[0])
        assert browser == {'search': 'orders', 'cursor': 6}
        assert result == {'text': 'SELECT 12X', 'sql_calls': 0}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        os.close(master)
        os.close(slave)
