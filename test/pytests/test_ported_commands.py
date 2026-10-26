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


class FakeExplainCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql):
        self.conn.executed.append(sql)
        if self.conn.error:
            raise self.conn.error
        self.description = [(name,) for name in self.conn.header]

    def fetchall(self):
        return self.conn.rows


class FakeExplainConnection:
    def __init__(self, header, rows, error=None):
        self.header = header
        self.rows = rows
        self.error = error
        self.executed: list[str] = []

    def cursor(self):
        return FakeExplainCursor(self)


class FakeExec:
    def __init__(self, conn):
        self.conn = conn

    def run(self, sql):
        raise AssertionError('F5 must not go through SQLExecute.run()')


class FakeMycli:
    def __init__(self, header=('id', 'type'), rows=((1, 'ALL'),), error=None):
        self.sqlexecute = FakeExec(FakeExplainConnection(list(header), list(rows), error))


def _explain(sql, cursor_position=None, **kwargs):
    from mycli.key_bindings import _run_explain, explainable_statement

    mycli = FakeMycli(**kwargs)
    statement = explainable_statement(sql, len(sql) if cursor_position is None else cursor_position)
    if statement is not None:
        _run_explain(mycli, statement)
    return mycli.sqlexecute.conn.executed


def test_f5_explain_prefixes_the_query_and_prints_a_table(capsys):
    assert _explain('SELECT * FROM t') == ['EXPLAIN SELECT * FROM t']
    out = capsys.readouterr().out
    assert 'EXPLAIN Preview (F5)' in out
    assert 'id' in out and 'ALL' in out


@pytest.mark.parametrize(
    ('cursor_position', 'expected'),
    [(0, 'EXPLAIN SELECT 1'), (9, 'EXPLAIN SELECT 1'), (12, 'EXPLAIN DELETE FROM t'), (None, 'EXPLAIN DELETE FROM t')],
)
def test_f5_explains_only_the_statement_under_the_cursor(cursor_position, expected):
    """`SELECT 1; DELETE FROM t` must never execute the DELETE itself."""
    assert _explain('SELECT 1; DELETE FROM t', cursor_position) == [expected]


@pytest.mark.parametrize(
    'sql',
    [
        'EXPLAIN SELECT 1',
        'EXPLAIN ANALYZE SELECT 1',
        'show tables',
        'use mydb',
        'desc t',
        'set x=1',
        'DROP TABLE t',
        '\\dt',
        '/dt',
        '\\fs q SELECT 1',
        '   ',
    ],
)
def test_f5_explain_skips_statements_it_cannot_explain(sql):
    assert _explain(sql) == []


def test_f5_explain_drops_comments_that_could_hide_a_second_statement():
    assert _explain('SELECT 1 /*!; DELETE FROM t */ -- x') == ['EXPLAIN SELECT 1']


def test_f5_explain_accepts_ctes_and_strips_terminators():
    assert _explain('WITH a AS (SELECT 1) SELECT * FROM a\\G') == ['EXPLAIN WITH a AS (SELECT 1) SELECT * FROM a']


def test_f5_explain_refuses_a_custom_delimiter_statement_holding_a_semicolon(monkeypatch):
    monkeypatch.setattr(iocommands.delimiter_command, '_delimiter', '$$')
    assert _explain('SELECT 1; DELETE FROM t$$') == []


def test_f5_explain_renders_nulls_and_survives_errors(capsys):
    _explain('SELECT 1', header=['a'], rows=[(None,)])
    assert 'NULL' in capsys.readouterr().out

    assert _explain('SELECT 1', error=RuntimeError('boom')) == ['EXPLAIN SELECT 1']  # must not raise


def test_f5_binding_runs_the_preview_in_the_terminal_not_on_a_thread(monkeypatch):
    from types import SimpleNamespace

    from prompt_toolkit.document import Document

    from mycli import key_bindings

    scheduled = []
    monkeypatch.setattr(key_bindings, 'run_in_terminal', scheduled.append)
    mycli = FakeMycli()
    stub = SimpleNamespace(config={}, sqlexecute=mycli.sqlexecute)
    binding = next(b for b in key_bindings.mycli_bindings(stub).bindings if [str(k) for k in b.keys] == ['Keys.F5'])
    buffer = SimpleNamespace(document=Document('SELECT 1; DELETE FROM t', 3))
    buffer.text, buffer.cursor_position = buffer.document.text, buffer.document.cursor_position

    binding.handler(SimpleNamespace(app=SimpleNamespace(current_buffer=buffer)))

    assert len(scheduled) == 1
    assert mycli.sqlexecute.conn.executed == []  # nothing ran until the terminal is free
    scheduled[0]()
    assert mycli.sqlexecute.conn.executed == ['EXPLAIN SELECT 1']


def make_cursor(rows, description=(('a', 3, None, None, None, None, None),), warning_count=0, cursor_class=None):
    """A real pymysql cursor holding a fetched result, without a server."""
    from pymysql.cursors import Cursor

    cursor = (cursor_class or Cursor)(None)
    cursor._executed = 'SELECT'
    cursor._rows = tuple(rows)
    cursor.rownumber = 0
    cursor.rowcount = len(rows)
    cursor.description = description
    cursor.warning_count = warning_count
    return cursor


def test_capture_stores_result_sets_despite_the_status_wording():
    """The status for a result set is "N rows in set", so is_select() is False.

    Keying the capture off is_select() silently stored nothing; this pins it.
    """
    from mycli.main_modes.repl import capture_last_result
    from mycli.packages.sqlresult import SQLResult

    cursor = make_cursor([(1,), (2,)], warning_count=1)
    result = SQLResult(header=['a'], rows=cursor, status='2 rows in set')
    capture_last_result(result, __import__('logging').getLogger('test'))

    assert get_last_result() == (['a'], [(1,), (2,)])
    # The formatter and the warnings check still get the cursor, rewound.
    assert result.rows is cursor
    assert cursor.warning_count == 1
    assert list(cursor) == [(1,), (2,)]


def test_numeric_column_stays_right_aligned_after_capture():
    from mycli.main_modes.repl import capture_last_result
    from mycli.output import OutputMixin
    from mycli.packages.sqlresult import SQLResult
    from test.utils import make_bare_mycli

    description = (('id', 3, None, None, None, None, None), ('name', 253, None, None, None, None, None))
    result = SQLResult(header=['id', 'name'], rows=make_cursor([(1, 'x'), (22, 'y')], description), status='2 rows in set')
    capture_last_result(result, __import__('logging').getLogger('test'))

    cli = make_bare_mycli()
    list(OutputMixin.format_sqlresult(cli, result, numeric_alignment='right'))

    (rows, _header, _format_name), kwargs = cli.main_formatter.calls[-1]
    assert kwargs['colalign'] == ['right', 'left']
    assert list(rows) == [(1, 'x'), (22, 'y')]


def test_capture_skips_unbuffered_cursors():
    """An SSCursor cannot rewind, and copying it would defeat --unbuffered."""
    from pymysql.cursors import SSCursor

    from mycli.main_modes.repl import capture_last_result
    from mycli.packages.sqlresult import SQLResult

    cursor = make_cursor([(1,)], cursor_class=SSCursor)
    capture_last_result(SQLResult(header=['a'], rows=cursor), __import__('logging').getLogger('test'))

    assert get_last_result() == (None, None)


def test_capture_of_an_empty_result_set():
    from mycli.main_modes.repl import capture_last_result
    from mycli.packages.sqlresult import SQLResult

    capture_last_result(SQLResult(header=['a'], rows=make_cursor([])), __import__('logging').getLogger('test'))

    assert get_last_result() == (['a'], [])


def test_capture_ignores_results_without_a_row_set():
    from mycli.main_modes.repl import capture_last_result
    from mycli.packages.sqlresult import SQLResult

    logger = __import__('logging').getLogger('test')
    capture_last_result(SQLResult(status='Query OK, 1 row affected'), logger)
    assert get_last_result() == (None, None)
    # Already-materialised rows (e.g. from \last itself) must not overwrite.
    capture_last_result(SQLResult(header=['a'], rows=[(9,)], status='1 row in set'), logger)
    assert get_last_result() == (None, None)
