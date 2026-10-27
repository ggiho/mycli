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


def test_f5_explain_drops_comments():
    assert _explain('SELECT 1 /* x */ -- y') == ['EXPLAIN SELECT 1']


@pytest.mark.parametrize('sql', ['SELECT 1 /*!; DELETE FROM t */', 'SELECT /*!80000 SQL_NO_CACHE */ 1', '/*!SELECT 1*/'])
def test_f5_explain_refuses_executable_comments(sql):
    """MySQL runs what `/*! */` holds, so stripping it would explain another statement."""
    assert _explain(sql) == []


@pytest.mark.parametrize(
    ('text', 'cursor_position', 'expected'),
    [
        ('SELECT 1;\nSELECT 2;\nSELECT 3', 10, 'EXPLAIN SELECT 2'),  # column 0 of statement 2
        ('SELECT 1;\nSELECT 2;\nSELECT 3', 20, 'EXPLAIN SELECT 3'),  # column 0 of statement 3
        ('SELECT 1;\nSELECT 2;\nSELECT 3', 0, 'EXPLAIN SELECT 1'),
        ('SELECT 1;\nSELECT 2;\nSELECT 3', 8, 'EXPLAIN SELECT 1'),  # on the `;`
        ('SELECT 1;\nSELECT 2;\nSELECT 3', 9, 'EXPLAIN SELECT 1'),  # in the line break after it
        ('SELECT 1;\nSELECT 2;\nSELECT 3', 28, 'EXPLAIN SELECT 3'),  # end of buffer
        ('select 1;select 2', 9, 'EXPLAIN select 2'),
        ('select 1;select 2', 8, 'EXPLAIN select 1'),
        ('select 1;select 2', 17, 'EXPLAIN select 2'),
        ('SELECT 1;   \n\n  SELECT 2', 12, 'EXPLAIN SELECT 1'),  # blanks between
        ('SELECT 1;   \n\n  SELECT 2', 16, 'EXPLAIN SELECT 2'),
        ('SELECT 1;;SELECT 2', 8, 'EXPLAIN SELECT 1'),  # an empty statement is skipped
        ('SELECT 1;\nSELECT 2;', 19, 'EXPLAIN SELECT 2'),  # end, after the last `;`
        ('  SELECT 1', 0, 'EXPLAIN SELECT 1'),
    ],
)
def test_f5_cursor_picks_the_statement_it_is_on(text, cursor_position, expected):
    assert _explain(text, cursor_position) == [expected]


def test_f5_explain_accepts_ctes_and_strips_terminators():
    assert _explain('WITH a AS (SELECT 1) SELECT * FROM a\\G') == ['EXPLAIN WITH a AS (SELECT 1) SELECT * FROM a']


def test_f5_explain_refuses_a_custom_delimiter_statement_holding_a_semicolon(monkeypatch):
    monkeypatch.setattr(iocommands.delimiter_command, '_delimiter', '$$')
    assert _explain('SELECT 1; DELETE FROM t$$') == []


def test_f5_explain_renders_nulls_and_survives_errors(capsys):
    _explain('SELECT 1', header=['a'], rows=[(None,)])
    assert 'NULL' in capsys.readouterr().out

    assert _explain('SELECT 1', error=RuntimeError('boom')) == ['EXPLAIN SELECT 1']  # must not raise


def _f5_bindings(mycli):
    from types import SimpleNamespace

    from mycli import key_bindings

    stub = SimpleNamespace(config={}, sqlexecute=mycli.sqlexecute)
    bindings = key_bindings.mycli_bindings(stub).bindings
    f5 = next(b for b in bindings if [str(k) for k in b.keys] == ['Keys.F5'])
    sigint = next(b for b in bindings if [str(k) for k in b.keys] == ['Keys.SIGINT'])
    return f5, sigint


def test_f5_binding_runs_the_preview_in_the_terminal_on_an_executor(monkeypatch):
    """In the terminal, so the prompt is out of the way; on an executor, so Ctrl-C reaches the event loop."""
    from types import SimpleNamespace

    from prompt_toolkit.document import Document

    from mycli import key_bindings

    scheduled = []
    monkeypatch.setattr(key_bindings, 'run_in_terminal', lambda func, **kwargs: scheduled.append((func, kwargs)))
    mycli = FakeMycli()
    f5, sigint = _f5_bindings(mycli)
    buffer = SimpleNamespace(document=Document('SELECT 1; DELETE FROM t', 3))
    buffer.text, buffer.cursor_position = buffer.document.text, buffer.document.cursor_position

    assert not sigint.filter()  # Ctrl-C keeps its usual meaning until F5
    f5.handler(SimpleNamespace(app=SimpleNamespace(current_buffer=buffer)))

    assert len(scheduled) == 1
    func, kwargs = scheduled[0]
    assert kwargs == {'in_executor': True}
    assert mycli.sqlexecute.conn.executed == []  # nothing ran until the terminal is free
    assert sigint.filter()  # Ctrl-C now cancels the EXPLAIN instead of clearing the buffer
    func()
    assert mycli.sqlexecute.conn.executed == ['EXPLAIN SELECT 1']
    assert not sigint.filter()


class FakeSideConnection:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return FakeExplainCursor(self)

    @property
    def executed(self):
        return self.log

    error = None
    header: list = []

    def close(self):
        self.log.append('close')


class FakeSQLExecute:
    def __init__(self, connection_id=42, connect_error=None):
        self.connection_id = connection_id
        self.connect_error = connect_error
        self.conn = 'the session connection'
        self.side_log: list[str] = []

    def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.conn = FakeSideConnection(self.side_log)


def test_ctrl_c_during_f5_kills_the_query_from_a_second_connection(capsys):
    from mycli.key_bindings import _cancel_explain

    sqlexecute = FakeSQLExecute()
    _cancel_explain(sqlexecute)

    assert sqlexecute.side_log == ['KILL QUERY 42', 'close']
    # The session's own connection is left alone, so it stays usable.
    assert sqlexecute.conn == 'the session connection'
    assert 'Cancelled query id: 42' in capsys.readouterr().err


def test_ctrl_c_during_f5_reports_what_it_could_not_cancel(capsys):
    from mycli.key_bindings import _cancel_explain

    _cancel_explain(FakeSQLExecute(connection_id=0))
    assert 'skip cancelling' in capsys.readouterr().err

    sqlexecute = FakeSQLExecute(connect_error=OSError('refused'))
    _cancel_explain(sqlexecute)
    assert 'refused' in capsys.readouterr().err
    assert sqlexecute.conn == 'the session connection'


def test_an_interrupted_f5_explain_prints_nothing_and_clears_the_flag(capsys):
    import pymysql

    from mycli import key_bindings

    key_bindings.EXPLAIN_RUNNING.set()
    _explain('SELECT 1', error=pymysql.err.OperationalError(1317, 'Query execution was interrupted'))
    assert not key_bindings.EXPLAIN_RUNNING.is_set()
    assert 'EXPLAIN Preview' not in capsys.readouterr().out
