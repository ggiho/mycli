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
config['favorite_queries']={'orders': 'SELECT * FROM orders', 'template': "SELECT $1, '{{ kv.name }}'"};config.write()
FavoriteQueries.instance=FavoriteQueries(config,sys.argv[1])
patch=MonkeyPatch();patch_repl_runtime_defaults(patch)
patch.setattr(repl,'render_prompt_string',lambda *args:ANSI('qa> '))
executor=SimpleNamespace(conn=Mock(),run=Mock(side_effect=AssertionError('unexpected SQL execution')),dbname='qa')
cli=make_repl_cli(executor);cli.prompt_session=PromptSession();cli.register_favorite_commands=Mock()
state=repl.ReplState()
repl._one_iteration(cli,state)
print('RESULT_JSON '+json.dumps({'buffer':state.buffer_text,'queries':dict(ConfigObj(sys.argv[1])['favorite_queries']),
 'sql_calls':executor.run.call_count,'cursor_calls':executor.conn.cursor.call_count,'refreshes':cli.register_favorite_commands.call_count}))
'''


@pytest.mark.parametrize('editing', [False, True])
def test_terminal_load_and_save_restore_screen_without_sql(tmp_path: Path, editing: bool) -> None:
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    initial = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    config = tmp_path / ('edit-config' if editing else 'load-config')
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
            os.write(master, b'/template\r\r')
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
            assert result['buffer'] == "SELECT $1, '{{ kv.name }}';" and result['refreshes'] == 0
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
