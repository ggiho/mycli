from __future__ import annotations

import itertools
import shutil
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from cli_helpers.utils import strip_ansi
import click
from configobj import ConfigObj
import prompt_toolkit
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_plain_text
from prompt_toolkit.utils import get_cwidth
from pygments.style import Style as PygmentsStyle
from pygments.token import Token
import pytest

from mycli import compat
from mycli import output as output_module
from mycli.output import OutputMixin
from mycli.packages.sqlresult import SQLResult
from mycli.types import ImageProtocol
from test.utils import DummyFormatter, FakeCursorBase, make_bare_mycli  # type: ignore[attr-defined]


def test_output_timing_logs_and_prints_with_default_style(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    logged: list[Any] = []
    printed: list[tuple[Any, Any]] = []
    cli.log_output = lambda value: logged.append(value)  # type: ignore[assignment]
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: printed.append((text, style)))

    OutputMixin.output_timing(cli, '0.12 sec')

    assert logged == ['0.12 sec']
    assert to_plain_text(printed[0][0]) == '0.12 sec'
    assert list(printed[0][0])[0][0].strip() == 'class:output.timing'
    assert printed[0][1] == cli.ptoolkit_style


def test_output_timing_uses_warning_style(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.log_output = lambda value: None  # type: ignore[assignment]
    printed: list[Any] = []
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: printed.append(text))

    OutputMixin.output_timing(cli, '0.34 sec', is_warnings_style=True)

    assert list(printed[0])[0][0].strip() == 'class:warnings.timing'


def test_log_query_and_log_output_write_plain_text(tmp_path) -> None:
    cli = make_bare_mycli()
    logfile = tmp_path / 'audit.log'

    with logfile.open('w+', encoding='utf-8') as handle:
        cli.logfile = handle
        OutputMixin.log_query(cli, 'select 1')
        OutputMixin.log_output(cli, ANSI('\x1b[31mhello\x1b[0m'))
        handle.seek(0)
        contents = handle.read()

    assert 'select 1' in contents
    assert 'hello' in contents
    assert '\x1b[31m' not in contents


def test_log_output_ignores_missing_logfile() -> None:
    cli = make_bare_mycli()
    cli.logfile = None

    OutputMixin.log_output(cli, 'nothing to write')


def test_echo_logs_and_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    logged: list[str] = []
    printed: list[tuple[str, dict[str, Any]]] = []
    cli.log_output = lambda value: logged.append(value)  # type: ignore[assignment]
    monkeypatch.setattr(click, 'secho', lambda value, **kwargs: printed.append((value, kwargs)))

    OutputMixin.echo(cli, 'message', fg='red')

    assert logged == ['message']
    assert printed == [('message', {'fg': 'red'})]


def test_output_emits_iterm2_image_without_text_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.explicit_pager = False
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    logged: list[Any] = []
    emitted: list[str] = []
    cli.log_output = lambda value: logged.append(value)  # type: ignore[assignment]
    monkeypatch.setattr(click, 'echo', lambda value=None, **_kwargs: emitted.append(value))

    OutputMixin.output(cli, itertools.chain(), SQLResult(image=b'png', image_protocol='iterm2'))

    assert logged == []
    assert emitted == ['\x1b]1337;File=name=Y2hhcnQucG5n;size=3;width=auto;height=auto;preserveAspectRatio=1;inline=1:cG5n\x07']


def test_output_emits_kitty_image_in_base64_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.explicit_pager = False
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    emitted: list[tuple[str | None, dict[str, Any]]] = []
    monkeypatch.setattr(click, 'echo', lambda value=None, **kwargs: emitted.append((value, kwargs)))
    image = b'x' * 4000

    OutputMixin.output(cli, itertools.chain(), SQLResult(image=image, image_protocol='kitty'))

    encoded = output_module.base64.b64encode(image).decode('ascii')
    assert emitted == [
        (f'\x1b_Ga=T,f=100,m=1;{encoded[:4096]}\x1b\\', {'nl': False}),
        (f'\x1b_Gm=0;{encoded[4096:]}\x1b\\', {'nl': False}),
        (None, {}),
    ]


@pytest.mark.parametrize('image_protocol', ['iterm2', 'kitty'])
def test_output_suppresses_images_in_windows_console(
    monkeypatch: pytest.MonkeyPatch,
    image_protocol: ImageProtocol,
) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = cast(
        Any,
        SimpleNamespace(output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=80, rows=24))),
    )
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    emitted: list[str | None] = []
    monkeypatch.setattr(output_module, 'is_windows_console', lambda output: True)
    monkeypatch.setattr(click, 'echo', lambda value=None, **_kwargs: emitted.append(value))

    OutputMixin.output(cli, itertools.chain(), SQLResult(image=b'png', image_protocol=image_protocol))

    assert emitted == []


def test_is_windows_console_detects_prompt_toolkit_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeWin32Output:
        pass

    class FakeWindows10Output:
        pass

    class FakeConEmuOutput:
        pass

    modules = (
        ('prompt_toolkit.output.win32', 'Win32Output', FakeWin32Output),
        ('prompt_toolkit.output.windows10', 'Windows10_Output', FakeWindows10Output),
        ('prompt_toolkit.output.conemu', 'ConEmuOutput', FakeConEmuOutput),
    )
    for module_name, class_name, output_class in modules:
        module = ModuleType(module_name)
        setattr(module, class_name, output_class)
        monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(compat.sys, 'platform', 'win32')

    assert compat.is_windows_console(FakeWin32Output())
    assert compat.is_windows_console(FakeWindows10Output())
    assert compat.is_windows_console(FakeConEmuOutput())
    assert not compat.is_windows_console(object())


def test_is_windows_console_ignores_non_windows_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat.sys, 'platform', 'cygwin')

    assert not compat.is_windows_console(object())


def test_get_output_margin_renders_prompt_once_and_counts_status_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_lines = 0
    cli.prompt_format = 'ignored'
    cli.prompt_session = None
    cli.get_reserved_space = lambda: 2  # type: ignore[assignment]
    monkeypatch.setattr(output_module.repl_mode, 'render_prompt_string', lambda *_args: FormattedText([('', 'one\ntwo')]))
    monkeypatch.setattr(output_module.special, 'is_timing_enabled', lambda: True)

    margin = OutputMixin.get_output_margin(cli, 'ok\nwarning')

    assert margin == 7
    assert cli.prompt_lines == 2


def test_get_output_margin_uses_prompt_session_render_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_lines = 0
    cli.prompt_session = cast(Any, SimpleNamespace(app=SimpleNamespace(render_counter=9)))
    cli.get_reserved_space = lambda: 1  # type: ignore[assignment]
    render_counters: list[int] = []

    def render_prompt_string(_cli: Any, _prompt_format: str, render_counter: int) -> FormattedText:
        render_counters.append(render_counter)
        return FormattedText([('', 'prompt')])

    monkeypatch.setattr(output_module.repl_mode, 'render_prompt_string', render_prompt_string)
    monkeypatch.setattr(output_module.special, 'is_timing_enabled', lambda: False)

    assert OutputMixin.get_output_margin(cli) == 2
    assert render_counters == [9]


def test_output_writes_lines_sinks_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.explicit_pager = False
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    logged: list[Any] = []
    tee: list[str] = []
    once: list[str] = []
    pipe_once: list[str] = []
    printed_lines: list[str] = []
    printed_status: list[Any] = []
    cli.log_output = lambda value: logged.append(value)  # type: ignore[assignment]
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: tee.append(value))
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: once.append(value))
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: pipe_once.append(value))
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_pager_enabled', lambda: False)
    monkeypatch.setattr(click, 'secho', lambda value, **_kwargs: printed_lines.append(value))
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: printed_status.append(text))

    OutputMixin.output(cli, itertools.chain(['row 1', 'row 2']), SQLResult(status='done'))

    assert logged == ['row 1', 'row 2', 'done']
    assert tee == ['row 1', 'row 2']
    assert once == ['row 1', 'row 2']
    assert pipe_once == ['row 1', 'row 2']
    assert printed_lines == ['row 1', 'row 2']
    assert to_plain_text(printed_status[0]) == 'done'
    assert list(printed_status[0])[0][0].strip() == 'class:output.status'


def test_output_uses_prompt_session_size(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = cast(
        Any,
        SimpleNamespace(output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=80, rows=24))),
    )
    cli.explicit_pager = False
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    printed_lines: list[str] = []
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_pager_enabled', lambda: False)
    monkeypatch.setattr(click, 'secho', lambda value, **_kwargs: printed_lines.append(value))

    OutputMixin.output(cli, itertools.chain(['row']), SQLResult())

    assert printed_lines == ['row']


def test_output_flushes_buffer_when_content_does_not_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.explicit_pager = False
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: output_module.DEFAULT_HEIGHT  # type: ignore[assignment]
    printed_lines: list[str] = []
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_pager_enabled', lambda: False)
    monkeypatch.setattr(click, 'secho', lambda value, **_kwargs: printed_lines.append(value))

    OutputMixin.output(cli, itertools.chain(['row 1', 'row 2']), SQLResult())

    assert printed_lines == ['row 1', 'row 2']


def test_output_switches_to_pager_when_content_does_not_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.explicit_pager = False
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: output_module.DEFAULT_HEIGHT  # type: ignore[assignment]
    paged_lines: list[str] = []
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_pager_enabled', lambda: True)
    monkeypatch.setattr(click, 'echo_via_pager', lambda values: paged_lines.extend(list(values)))

    OutputMixin.output(cli, itertools.chain(['row']), SQLResult())

    assert paged_lines == ['row\n']


def test_output_redirected_skips_screen_printing(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    printed_lines: list[str] = []
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: True)
    monkeypatch.setattr(output_module.special, 'is_pager_enabled', lambda: False)
    monkeypatch.setattr(click, 'secho', lambda value, **_kwargs: printed_lines.append(value))

    OutputMixin.output(cli, itertools.chain(['row']), SQLResult())

    assert printed_lines == []


def test_output_uses_warning_status_style(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    printed_status: list[Any] = []
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: printed_status.append(text))

    OutputMixin.output(cli, itertools.chain([]), SQLResult(status='warning'), is_warnings_style=True)

    assert list(printed_status[0])[0][0].strip() == 'class:warnings.status'


def test_output_preserves_formatted_status(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    printed_status: list[Any] = []
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: printed_status.append(text))
    status = FormattedText([('class:custom', 'formatted')])

    OutputMixin.output(cli, itertools.chain([]), SQLResult(status=status))

    assert to_plain_text(printed_status[0]) == 'formatted'


def test_output_sends_buffer_to_pager_when_pager_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.explicit_pager = True
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    paged_lines: list[str] = []
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_pager_enabled', lambda: True)
    monkeypatch.setattr(click, 'echo_via_pager', lambda values: paged_lines.extend(list(values)))
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: None)

    OutputMixin.output(cli, itertools.chain(['row 1', 'row 2']), SQLResult())

    assert paged_lines == ['row 1\n', 'row 2\n']


def test_output_sends_buffer_to_explorer_and_clears_temporary_pager(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.explicit_pager = False
    cli.explorer_command = 'explorer-cmd'
    cli.explorer_exists = lambda: True  # type: ignore[method-assign]
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    paged_lines: list[str] = []
    monkeypatch.delenv('PAGER', raising=False)
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_explorer_output', lambda: True)
    monkeypatch.setattr(click, 'echo_via_pager', lambda values: paged_lines.extend(list(values)))
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: None)

    OutputMixin.output(cli, itertools.chain(['row 1', 'row 2']), SQLResult())

    assert paged_lines == ['row 1\n', 'row 2\n']
    assert 'PAGER' not in output_module.os.environ


def test_output_sends_buffer_to_explorer_and_restores_existing_pager(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.explicit_pager = False
    cli.explorer_command = 'explorer-cmd'
    cli.explorer_exists = lambda: True  # type: ignore[method-assign]
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    paged_lines: list[str] = []
    monkeypatch.setenv('PAGER', 'original-pager')
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_explorer_output', lambda: True)
    monkeypatch.setattr(click, 'echo_via_pager', lambda values: paged_lines.extend(list(values)))
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: None)

    OutputMixin.output(cli, itertools.chain(['row']), SQLResult())

    assert paged_lines == ['row\n']
    assert output_module.os.environ['PAGER'] == 'original-pager'


def test_output_reports_missing_configured_explorer(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.prompt_session = None
    cli.explicit_pager = False
    cli.explorer_command = 'missing-explorer'
    cli.explorer_exists = lambda: False  # type: ignore[method-assign]
    cli.log_output = lambda value: None  # type: ignore[assignment]
    cli.get_output_margin = lambda status=None: 1  # type: ignore[assignment]
    printed_lines: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(output_module.special, 'write_tee', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'write_pipe_once', lambda value: None)
    monkeypatch.setattr(output_module.special, 'is_redirected', lambda: False)
    monkeypatch.setattr(output_module.special, 'is_explorer_output', lambda: True)
    monkeypatch.setattr(click, 'secho', lambda value, **kwargs: printed_lines.append((value, kwargs)))
    monkeypatch.setattr(prompt_toolkit, 'print_formatted_text', lambda text, style=None: None)

    OutputMixin.output(cli, itertools.chain(['row']), SQLResult())

    assert printed_lines == [('Configured explorer command not found: missing-explorer.', {'err': True, 'fg': 'red'})]


def test_configure_pager_uses_more_for_missing_less_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.config = ConfigObj({'main': {'pager': 'less', 'enable_pager': 'True'}})
    pager_calls: list[str] = []
    monkeypatch.setattr(output_module, 'WIN', True)
    monkeypatch.setattr(output_module.shutil, 'which', lambda value: None)
    monkeypatch.setattr(output_module.special, 'set_pager', lambda value: pager_calls.append(value))
    monkeypatch.setattr(output_module.special, 'disable_pager', lambda: None)

    OutputMixin.configure_pager(cli)

    assert pager_calls == ['more']


def test_configure_pager_uses_myclirc_pager_and_sets_less(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.config = ConfigObj({'main': {'pager': 'config-pager', 'enable_pager': 'True'}})
    pager_calls: list[str] = []
    disabled: list[bool] = []
    monkeypatch.delenv('LESS', raising=False)
    monkeypatch.setattr(output_module.special, 'set_pager', lambda value: pager_calls.append(value))
    monkeypatch.setattr(output_module.special, 'disable_pager', lambda: disabled.append(True))

    OutputMixin.configure_pager(cli)

    assert pager_calls == ['config-pager']
    assert disabled == []
    assert cli.explicit_pager is True
    assert output_module.os.environ['LESS'] == '-RXF'


def test_configure_pager_disables_pager_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.config = ConfigObj({'main': {'pager': '', 'enable_pager': 'False'}})
    pager_calls: list[str] = []
    disabled: list[bool] = []
    monkeypatch.setattr(output_module.special, 'set_pager', lambda value: pager_calls.append(value))
    monkeypatch.setattr(output_module.special, 'disable_pager', lambda: disabled.append(True))

    OutputMixin.configure_pager(cli)

    assert pager_calls == []
    assert disabled == [True]
    assert cli.explicit_pager is False


def test_explorer_exists_checks_command_name(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.explorer_command = 'explorer-cmd --with-args'
    checked: list[str] = []
    monkeypatch.setattr(output_module.shutil, 'which', lambda value: checked.append(value) or '/bin/explorer-cmd')  # type: ignore[func-returns-value]

    assert OutputMixin.explorer_exists(cli) is True
    assert checked == ['explorer-cmd']


def test_explorer_exists_returns_false_without_command() -> None:
    cli = make_bare_mycli()
    cli.explorer_command = ''

    assert OutputMixin.explorer_exists(cli) is False


def test_format_sqlresult_uses_redirect_formatter_and_appends_preamble_postamble() -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    cli.redirect_formatter = DummyFormatter()
    result = SQLResult(preamble='before', header=['id'], rows=[(1,)], postamble='after')

    formatted = list(OutputMixin.format_sqlresult(cli, result, is_redirected=True))

    assert formatted == ['before', 'plain output', 'after']
    assert cli.main_formatter.calls == []
    assert cli.redirect_formatter.calls


def test_format_sqlresult_uses_explorer_formatter_and_trims_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    cli.explorer_formatter = DummyFormatter()
    cli.explorer_trim_footer = True
    monkeypatch.setattr(output_module.special, 'is_explorer_output', lambda: True)
    cli.explorer_formatter.format_output = lambda *args, **kwargs: ['header', 'row', 'footer']  # type: ignore[method-assign]
    result = SQLResult(header=['id'], rows=[(1,)])

    formatted = list(OutputMixin.format_sqlresult(cli, result))

    assert formatted == ['header', 'row']
    assert cli.main_formatter.calls == []


def test_format_sqlresult_uses_null_string_when_default_missing_value_is_configured() -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    result = SQLResult(header=['id'], rows=[(None,)])

    list(OutputMixin.format_sqlresult(cli, result, null_string='<null>'))

    _, kwargs = cli.main_formatter.calls[-1]
    assert kwargs['missing_value'] == '<null>'


def test_format_sqlresult_for_cursor_sets_column_types_and_alignment(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    monkeypatch.setattr(output_module, 'Cursor', FakeCursorBase)
    rows = FakeCursorBase(rows=[(1, 'name')], rowcount=1, description=[('id', 3), ('name', 253)])
    result = SQLResult(header=['id', 'name'], rows=cast(Any, rows))

    assert list(OutputMixin.format_sqlresult(cli, result, numeric_alignment='left')) == ['plain output']

    _, kwargs = cli.main_formatter.calls[-1]
    assert kwargs['column_types'] == [int, str]
    assert kwargs['colalign'] == ['left', 'left']


def test_format_sqlresult_for_empty_cursor_uses_empty_column_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    monkeypatch.setattr(output_module, 'Cursor', FakeCursorBase)
    rows = FakeCursorBase(rowcount=0, description=[('id', 3)])
    result = SQLResult(header=['id'], rows=cast(Any, rows))

    list(OutputMixin.format_sqlresult(cli, result))

    _, kwargs = cli.main_formatter.calls[-1]
    assert kwargs['column_types'] == []
    assert kwargs['colalign'] == []


def test_format_sqlresult_materializes_cursor_rows_when_width_is_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    monkeypatch.setattr(output_module, 'Cursor', FakeCursorBase)
    rows = FakeCursorBase(rows=[(1,)], rowcount=1, description=[('id', 3)])
    result = SQLResult(header=['id'], rows=cast(Any, rows))

    list(OutputMixin.format_sqlresult(cli, result, max_width=100))

    formatted_rows = cli.main_formatter.calls[-1][0][0]
    assert formatted_rows == [(1,)]


def test_format_sqlresult_splits_string_formatter_output() -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    cli.main_formatter.format_output = lambda *args, **kwargs: 'one\ntwo'  # type: ignore[method-assign]
    result = SQLResult(header=['id'], rows=[(1,)])

    assert list(OutputMixin.format_sqlresult(cli, result)) == ['one', 'two']


def test_format_sqlresult_switches_to_vertical_when_first_line_is_too_wide() -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    result = SQLResult(header=['id'], rows=[(1,)])

    assert list(OutputMixin.format_sqlresult(cli, result, max_width=2)) == ['vertical output']


def test_format_sqlresult_splits_string_vertical_output_when_table_is_too_wide() -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()

    def format_output(rows, header, format_name=None, **kwargs):
        if format_name == 'vertical':
            return 'vertical one\nvertical two'
        return ['too wide']

    cli.main_formatter.format_output = format_output  # type: ignore[method-assign]
    result = SQLResult(header=['id'], rows=[(1,)])

    assert list(OutputMixin.format_sqlresult(cli, result, max_width=2)) == ['vertical one', 'vertical two']


def test_format_sqlresult_keeps_table_when_first_line_fits_width() -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter()
    result = SQLResult(header=['id'], rows=[(1,)])

    assert list(OutputMixin.format_sqlresult(cli, result, max_width=100)) == ['plain output']


def test_format_as_grid_fills_columns_top_to_bottom_like_ls() -> None:
    rows = [(name,) for name in ['alpha', 'b', 'charlie', 'd', 'echo']]

    # Three columns of two rows need 5 + 2 + 7 + 2 + 4 = 20 cells plus 4 for the
    # frame; anything narrower falls back to two columns of three rows.
    assert output_module.format_as_grid('Database', rows, 24, '<null>') == [
        '+----------------------+',
        '| Database             |',
        '+----------------------+',
        '| alpha  charlie  echo |',
        '| b      d             |',
        '+----------------------+',
    ]
    assert output_module.format_as_grid('Database', rows, 23, '<null>') == [
        '+---------------+',
        '| Database      |',
        '+---------------+',
        '| alpha    d    |',
        '| b        echo |',
        '| charlie       |',
        '+---------------+',
    ]


def test_format_as_grid_widens_the_frame_for_a_long_header() -> None:
    assert output_module.format_as_grid('Tables_in_db', [('a',), ('b',)], 80, '<null>') == [
        '+--------------+',
        '| Tables_in_db |',
        '+--------------+',
        '| a  b         |',
        '+--------------+',
    ]


def test_format_as_grid_uses_a_box_drawing_frame_for_unicode_formats() -> None:
    assert output_module.format_as_grid('h', [('a',), ('b',)], 80, '<null>', unicode_frame=True) == [
        '┌──────┐',
        '│ h    │',
        '├──────┤',
        '│ a  b │',
        '└──────┘',
    ]


def test_format_as_grid_styles_header_separators_and_nulls() -> None:
    class GridStyle(PygmentsStyle):
        styles = {Token.Output.Header: 'bold', Token.Output.TableSeparator: '#888888', Token.Output.Null: 'italic'}

    lines = output_module.format_as_grid('h', [('a',), (None,)], 80, '<null>', style=GridStyle)

    assert lines is not None
    assert [strip_ansi(line) for line in lines] == ['+-----------+', '| h         |', '+-----------+', '| a  <null> |', '+-----------+']
    assert all(line != strip_ansi(line) for line in lines)


def test_format_as_grid_returns_none_when_two_columns_do_not_fit() -> None:
    rows = [('x' * 10,), ('y' * 10,)]
    assert output_module.format_as_grid('h', rows, 25, '<null>') is None
    assert output_module.format_as_grid('h', rows, 26, '<null>') == [
        '+------------------------+',
        '| h                      |',
        '+------------------------+',
        '| xxxxxxxxxx  yyyyyyyyyy |',
        '+------------------------+',
    ]


def test_format_as_grid_measures_wide_characters_and_shows_missing_values() -> None:
    lines = output_module.format_as_grid('h', [('한글',), (None,), ('a',)], 80, '<null>')

    assert lines is not None
    assert lines[3] == '| 한글  <null>  a |'
    assert len({get_cwidth(line) for line in lines}) == 1


@pytest.mark.parametrize('rows', [[(b'x',), ('y',)], [('a\nb',), ('c',)], [('a\tb',), ('c',)], [('only',)]])
def test_format_as_grid_returns_none_for_values_it_cannot_lay_out(rows: list) -> None:
    assert output_module.format_as_grid('h', rows, 80, '<null>') is None


def test_format_sqlresult_lays_out_single_column_as_grid_when_width_is_given() -> None:
    cli = make_bare_mycli()
    result = SQLResult(header=['Database'], rows=[('a',), ('b',), ('c',)])

    assert [strip_ansi(line) for line in OutputMixin.format_sqlresult(cli, result, grid_width=80)] == [
        '+----------+',
        '| Database |',
        '+----------+',
        '| a  b  c  |',
        '+----------+',
    ]
    assert cli.main_formatter.calls == []


@pytest.mark.parametrize(
    ('header', 'kwargs'),
    [
        (['Database'], {}),
        (['Database'], {'grid_width': 80, 'is_expanded': True}),
        (['Database'], {'grid_width': 80, 'is_redirected': True}),
        (['id', 'name'], {'grid_width': 80}),
    ],
)
def test_format_sqlresult_keeps_table_unless_single_column_on_screen(header: list[str], kwargs: dict[str, Any]) -> None:
    cli = make_bare_mycli()
    rows = [('a',) * len(header), ('b',) * len(header)]

    formatted = list(OutputMixin.format_sqlresult(cli, SQLResult(header=header, rows=rows), **kwargs))

    assert formatted in (['plain output'], ['vertical output'])


def test_format_sqlresult_keeps_table_for_data_formats() -> None:
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter(format_name='csv')
    result = SQLResult(header=['Database'], rows=[('a',), ('b',)])

    assert list(OutputMixin.format_sqlresult(cli, result, grid_width=80)) == ['plain output']


@pytest.mark.parametrize('format_name', ['plain', 'simple', 'minimal', 'github', 'pipe', 'rst', 'orgtbl'])
def test_format_sqlresult_keeps_borderless_formats_free_of_a_grid_frame(format_name: str) -> None:
    """A boxed frame would stop github output being Markdown, and so on."""
    cli = make_bare_mycli()
    cli.main_formatter = DummyFormatter(format_name=format_name)
    cli.main_formatter._output_formats[format_name] = cli.main_formatter._output_formats['ascii']
    result = SQLResult(header=['Database'], rows=[('a',), ('b',)])

    assert list(OutputMixin.format_sqlresult(cli, result, grid_width=80)) == ['plain output']


def test_format_sqlresult_materializes_cursor_rows_for_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    rows = FakeCursorBase(rows=[('a',), ('b',)], rowcount=2, description=[('Database', 253)])
    monkeypatch.setattr(output_module, 'Cursor', FakeCursorBase)
    result = SQLResult(header=['Database'], rows=cast(Any, rows))

    assert strip_ansi(list(OutputMixin.format_sqlresult(cli, result, grid_width=80))[3]) == '| a  b     |'


def test_get_reserved_space_caps_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = make_bare_mycli()
    monkeypatch.setattr(shutil, 'get_terminal_size', lambda *args, **kwargs: (120, 40))

    assert OutputMixin.get_reserved_space(cli) == 8
