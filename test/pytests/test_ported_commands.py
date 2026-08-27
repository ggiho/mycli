"""Commands carried over from the 1.x fork: \\last, \\copy, \\fq and F5 EXPLAIN."""

import pytest

from mycli.packages.special import iocommands
from mycli.packages.special.iocommands import (
    copy_last_result,
    get_last_result,
    show_last_result,
    store_last_result,
    toggle_favorite_query_display,
)


@pytest.fixture(autouse=True)
def _reset_state():
    header, rows = get_last_result()
    show = iocommands.is_show_favorite_query()
    store_last_result(None, None)
    yield
    store_last_result(header, rows)
    iocommands.set_show_favorite_query(show)


def test_last_reports_when_there_is_nothing_yet():
    assert show_last_result()[0].status == 'No previous result.'


def test_last_redisplays_the_stored_rows():
    store_last_result(['a', 'b'], [(1, 2), (3, 4)])
    result = show_last_result()[0]
    assert result.header == ['a', 'b']
    assert result.rows == [(1, 2), (3, 4)]
    assert '2 row(s)' in result.status


def test_copy_reports_when_there_is_nothing_yet():
    assert copy_last_result()[0].status == 'No previous result to copy.'


def test_copy_writes_tsv_with_nulls_spelled_out(monkeypatch):
    copied = {}
    monkeypatch.setattr(iocommands.pyperclip, 'copy', lambda text: copied.setdefault('text', text))
    store_last_result(['a', 'b'], [(1, None)])
    status = copy_last_result()[0].status
    assert copied['text'] == 'a\tb\n1\tNULL'
    assert '1 row(s)' in status


def test_copy_surfaces_clipboard_failures(monkeypatch):
    def boom(_text):
        raise RuntimeError('no clipboard')

    monkeypatch.setattr(iocommands.pyperclip, 'copy', boom)
    store_last_result(['a'], [(1,)])
    result = copy_last_result()[0]
    assert result.is_error is True
    assert 'no clipboard' in result.status


def test_fq_toggles_and_reports_the_new_state():
    iocommands.set_show_favorite_query(False)
    assert 'ON' in toggle_favorite_query_display()[0].status
    assert iocommands.is_show_favorite_query() is True
    assert 'OFF' in toggle_favorite_query_display()[0].status
    assert iocommands.is_show_favorite_query() is False


class FakeResult:
    def __init__(self, header, rows):
        self.header = header
        self.rows = rows


class FakeExec:
    def __init__(self, results):
        self.results = results
        self.ran = None

    def run(self, sql):
        self.ran = sql
        return self.results


class FakeMycli:
    def __init__(self, results):
        self.sqlexecute = FakeExec(results)
        self.logger = __import__('logging').getLogger('test')


class FakeApp:
    def __init__(self):
        self.invalidated = False

    def invalidate(self):
        self.invalidated = True


def _explain(sql, results=None, capsys=None):
    from mycli.key_bindings import _run_explain

    mycli = FakeMycli(results if results is not None else [FakeResult(['id', 'type'], [(1, 'ALL')])])
    app = FakeApp()
    _run_explain(mycli, sql, app)
    return mycli, app


def test_f5_explain_prefixes_the_query_and_prints_a_table(capsys):
    mycli, app = _explain('SELECT * FROM t')
    assert mycli.sqlexecute.ran == 'EXPLAIN SELECT * FROM t'
    out = capsys.readouterr().out
    assert 'EXPLAIN Preview (F5)' in out
    assert 'id' in out and 'ALL' in out
    assert app.invalidated is True


@pytest.mark.parametrize(
    'sql',
    ['EXPLAIN SELECT 1', 'show tables', 'use mydb', 'desc t', 'set x=1', '\\dt', '/dt', '   '],
)
def test_f5_explain_skips_statements_it_cannot_explain(sql):
    mycli, _ = _explain(sql)
    assert mycli.sqlexecute.ran is None


def test_f5_explain_renders_nulls_and_survives_errors(capsys):
    _explain('SELECT 1', results=[FakeResult(['a'], [(None,)])])
    assert 'NULL' in capsys.readouterr().out

    class Boom(FakeExec):
        def run(self, sql):
            raise RuntimeError('boom')

    from mycli.key_bindings import _run_explain

    mycli = FakeMycli([])
    mycli.sqlexecute = Boom([])
    _run_explain(mycli, 'SELECT 1', FakeApp())  # must not raise


class FakeCursor:
    """Minimal Cursor stand-in; capture_last_result checks isinstance(Cursor)."""

    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


def test_capture_stores_result_sets_despite_the_status_wording():
    """The status for a result set is "N rows in set", so is_select() is False.

    Keying the capture off is_select() silently stored nothing; this pins it.
    """
    from unittest.mock import patch

    from mycli.packages.sqlresult import SQLResult

    with patch('mycli.main_modes.repl.Cursor', FakeCursor):
        from mycli.main_modes.repl import capture_last_result

        result = SQLResult(header=['a'], rows=FakeCursor([(1,), (2,)]), status='2 rows in set')
        capture_last_result(result, __import__('logging').getLogger('test'))

    assert get_last_result() == (['a'], [(1,), (2,)])
    # The cursor was drained, so the formatter must get the list.
    assert result.rows == [(1,), (2,)]


def test_capture_ignores_results_without_a_row_set():
    from mycli.main_modes.repl import capture_last_result
    from mycli.packages.sqlresult import SQLResult

    logger = __import__('logging').getLogger('test')
    capture_last_result(SQLResult(status='Query OK, 1 row affected'), logger)
    assert get_last_result() == (None, None)
    # Already-materialised rows (e.g. from \last itself) must not overwrite.
    capture_last_result(SQLResult(header=['a'], rows=[(9,)], status='1 row in set'), logger)
    assert get_last_result() == (None, None)
