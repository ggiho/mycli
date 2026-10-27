import subprocess

import pytest

from mycli.packages.ptoolkit.atuin import ACCEPT_PREFIX, search_history, session_id
from mycli.packages.ptoolkit.history import AtuinHistory, FileHistoryWithTimestamp


class FakeAtuin:
    """Stands in for the atuin executable, recording argv and replaying output.

    `--cmd-only` searches, `--format` searches and the picker get separate
    canned output, since one history object issues all three.
    """

    def __init__(self, stdout='', formatted=None, stderr='', returncode=0, raises=None):
        self.stdout = stdout
        self.formatted = formatted
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.calls: list[list[str]] = []
        self.env: dict | None = None
        self.kwargs: dict = {}

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.env = kwargs.get('env')
        self.kwargs = kwargs
        if self.raises:
            raise self.raises
        out = self.formatted if ('--format' in argv and self.formatted is not None) else self.stdout
        return subprocess.CompletedProcess(argv, self.returncode, stdout=out, stderr=self.stderr)

    def popen(self, argv, **kwargs):
        """`history end` is started, not run: record it, but return nothing to wait on."""
        self.calls.append(list(argv))
        self.popen_kwargs = kwargs
        if self.raises:
            raise self.raises
        return None


@pytest.fixture
def fake_atuin(monkeypatch):
    def install(**kwargs):
        fake = FakeAtuin(**kwargs)
        monkeypatch.setattr(subprocess, 'run', fake)
        monkeypatch.setattr(subprocess, 'Popen', fake.popen)
        return fake

    return install


@pytest.fixture
def history(tmp_path):
    def build(**kwargs):
        # frecency off: its background thread would fire extra atuin calls.
        return AtuinHistory(tmp_path / 'hist', frecency_history_entries=0, **kwargs)

    return build


def test_subclasses_file_history_so_frecency_and_fzf_still_apply(history):
    """rehash and the fzf picker both dispatch on FileHistoryWithTimestamp."""
    h = history()
    assert isinstance(h, FileHistoryWithTimestamp)
    assert hasattr(h, 'refresh_frecency')
    assert hasattr(h, 'load_history_with_timestamp')


def test_session_id_is_stable_and_author_specific():
    assert session_id('mycli') == session_id('mycli')
    assert session_id('mycli') != session_id('other')
    assert len(session_id('mycli')) == 32
    assert all(c in '0123456789abcdef' for c in session_id('mycli'))


def test_session_id_differs_from_pgclis_scheme():
    """pgcli and mycli must not share a session, or each picker shows the other."""
    import hashlib

    pgcli = hashlib.sha256(b'pgcli-history:pgcli').hexdigest()[:32]
    assert session_id('mycli') != pgcli


def test_load_is_newest_first_and_keeps_duplicates(fake_atuin, history):
    fake = fake_atuin(stdout='SELECT 1\0SELECT 1\0SELECT 2\0')
    assert list(history().load_history_strings()) == ['SELECT 2', 'SELECT 1', 'SELECT 1']
    argv = fake.calls[0]
    assert argv[:2] == ['atuin', 'search']
    assert argv[argv.index('--author') + 1] == 'mycli'
    # A user's filter_mode (session, directory, ...) would otherwise hide every entry.
    assert argv[argv.index('--filter-mode') + 1] == 'global'
    # Duplicates feed the frecency score.
    assert '--include-duplicates' in argv
    # --reverse would make --limit pick the oldest entries; the order is flipped here instead.
    assert '--reverse' not in argv
    assert argv[argv.index('--limit') + 1] == '5000'
    assert '--cmd-only' in argv


def test_load_preserves_multiline_entries(fake_atuin, history):
    fake_atuin(stdout='SELECT 1\0SELECT a,\n       b\n  FROM t\0')
    assert list(history().load_history_strings()) == ['SELECT a,\n       b\n  FROM t', 'SELECT 1']


def test_history_file_is_appended_as_the_tail(fake_atuin, history, tmp_path):
    (tmp_path / 'hist').write_text('\n# 2020-01-01 00:00:00\n+old query\n', encoding='utf-8')
    fake_atuin(stdout='new query\0')
    assert list(history().load_history_strings()) == ['new query', 'old query']


def test_falls_back_to_the_file_when_atuin_fails(fake_atuin, history, tmp_path):
    (tmp_path / 'hist').write_text('\n# 2020-01-01 00:00:00\n+old query\n', encoding='utf-8')
    fake_atuin(returncode=1)
    assert list(history().load_history_strings()) == ['old query']


def test_store_tags_author_and_session(fake_atuin, history):
    fake = fake_atuin(stdout='01a040f0cb257242b718f2ab411b84ce\n')
    history(author='mycli-test').store_string('SELECT 1')
    # `--` keeps SQL that starts with `-` (a leading comment) from parsing as an option.
    assert fake.calls == [['atuin', 'history', 'start', '--author', 'mycli-test', '--', 'SELECT 1']]
    assert fake.env['ATUIN_SESSION'] == session_id('mycli-test')


def test_sql_starting_with_a_comment_follows_the_separator(fake_atuin, history):
    fake = fake_atuin(stdout='id\n')
    history().store_string('-- why\nSELECT 1')
    argv = fake.calls[0]
    assert argv[argv.index('--') + 1] == '-- why\nSELECT 1'


def test_finish_ends_the_started_entry_with_its_id(fake_atuin, history):
    fake = fake_atuin(stdout='01a040f0cb257242b718f2ab411b84ce\n')
    h = history()
    h.store_string('SELECT 1')
    h.finish(1)

    end = fake.calls[1]
    assert end[:3] == ['atuin', 'history', 'end']
    assert end[end.index('--exit') + 1] == '1'
    assert int(end[end.index('--duration') + 1]) >= 0
    assert end[-2:] == ['--', '01a040f0cb257242b718f2ab411b84ce']
    h.finish(0)  # already ended
    assert len(fake.calls) == 2


def test_a_second_store_ends_the_entry_before_it(fake_atuin, history):
    """\\e and \\llm prompt again within one REPL iteration."""
    fake = fake_atuin(stdout='first\n')
    h = history()
    h.store_string('\\llm show tables')
    fake.stdout = 'second\n'
    h.store_string('SHOW TABLES')

    assert [call[2] for call in fake.calls] == ['start', 'end', 'start']
    assert fake.calls[1][fake.calls[1].index('--exit') + 1] == '0'
    assert fake.calls[1][-1] == 'first'


def test_a_failed_start_is_not_ended(fake_atuin, history):
    fake = fake_atuin(returncode=2, stderr='error: unexpected argument')
    h = history()
    h.store_string('SELECT 1')
    h.finish(0)
    assert [call[2] for call in fake.calls] == ['start']


def test_password_changes_are_not_sent_to_atuin(fake_atuin, history):
    """append_string() inherits the keep_out_of_history filter."""
    fake = fake_atuin(stdout='id\n')
    h = history()
    h.append_string("ALTER USER 'bob' IDENTIFIED BY 'Secret1!'")
    assert fake.calls == []
    h.append_string('SELECT 1')
    assert len(fake.calls) == 1


def test_u_builds_results_with_the_fields_sqlresult_actually_has():
    """A live run caught `results=`/`headers=` here after 2.x renamed them."""
    from mycli.packages.special.dbcommands import list_users

    class Cur:
        description = (('user',), ('host',))

        def execute(self, query):
            self.query = query

        def fetchall(self):
            return [('appuser', '%')]

    results = list_users(cur=Cur(), arg=None)
    assert results[0].header == ['user', 'host']
    assert results[0].rows == [('appuser', '%')]


def test_timestamps_split_on_the_first_tab_only(fake_atuin, history):
    """A tab inside the SQL must not shift the split, so {command} goes last."""
    fake_atuin(stdout='', formatted='2026-08-27 11:05:09\tSELECT\ta,\tb\0')
    assert history().load_history_with_timestamp() == [('SELECT\ta,\tb', '2026-08-27 11:05:09')]


def test_record_without_separator_is_kept_as_a_command(fake_atuin, history):
    fake_atuin(stdout='', formatted='SELECT 1\0')
    assert history().load_history_with_timestamp() == [('SELECT 1', '')]


def test_survives_a_missing_or_wedged_executable(fake_atuin, history, tmp_path):
    for boom in (FileNotFoundError('atuin'), subprocess.TimeoutExpired('atuin', 5)):
        (tmp_path / 'hist').unlink(missing_ok=True)
        fake_atuin(raises=boom)
        h = history()
        assert list(h.load_history_strings()) == []
        h.store_string('SELECT 1')  # must not raise
        h.finish(0)  # nothing pending: must not raise either


class FakeBuffer:
    def __init__(self, text='', complete_state=None):
        self.text = text
        self.cursor_position = len(text)
        self.complete_state = complete_state
        self.accepted = False

    def validate_and_handle(self):
        self.accepted = True

    def auto_up(self, count=1):
        pass


class FakeEvent:
    def __init__(self, buffer):
        self.current_buffer = buffer
        self.app = type(
            'App',
            (),
            {'renderer': type('R', (), {'reset': lambda self: None})(), 'invalidate': lambda self: None},
        )()
        self.arg = 1


def test_picker_seeds_the_query_and_keeps_stdout_on_the_terminal(fake_atuin):
    fake = fake_atuin(stderr='SELECT 1\n')
    buffer = FakeBuffer('SEL')
    assert search_history(FakeEvent(buffer), 'mycli', up_key_binding=True) is True

    argv = fake.calls[0]
    assert argv[:3] == ['atuin', 'search', '-i']
    assert '--shell-up-key-binding' in argv
    # The picker ignores --author, so scoping rides on the synthetic session.
    assert argv[argv.index('--filter-mode') + 1] == 'session'
    assert '--author' not in argv
    assert fake.env['ATUIN_SESSION'] == session_id('mycli')
    assert fake.env['ATUIN_QUERY'] == 'SEL'
    # The TUI draws on stdout; only the pick is captured.
    assert fake.kwargs['stdout'] is None
    assert fake.kwargs['stderr'] is subprocess.PIPE

    assert buffer.text == 'SELECT 1'
    assert buffer.accepted is False


def test_enter_accept_marker_runs_the_query(fake_atuin):
    fake_atuin(stderr=ACCEPT_PREFIX + 'SELECT 1\n')
    buffer = FakeBuffer()
    search_history(FakeEvent(buffer), 'mycli')
    assert buffer.text == 'SELECT 1'
    assert buffer.accepted is True


def test_escape_leaves_the_buffer_alone(fake_atuin):
    fake_atuin(stderr='\n')
    buffer = FakeBuffer('SELECT untouched')
    assert search_history(FakeEvent(buffer), 'mycli') is True
    assert buffer.text == 'SELECT untouched'


def test_reports_failure_so_the_caller_can_fall_back(fake_atuin):
    fake_atuin(raises=FileNotFoundError('atuin'))
    assert search_history(FakeEvent(FakeBuffer('SELECT 1')), 'mycli') is False


def test_a_failing_picker_is_not_pasted_into_the_buffer(fake_atuin):
    """On failure stderr carries atuin's error message, not a pick."""
    fake_atuin(returncode=1, stderr='Error: failed to deserialize config\n')
    buffer = FakeBuffer('SEL')
    assert search_history(FakeEvent(buffer), 'mycli', up_key_binding=True) is False
    assert buffer.text == 'SEL'
    assert buffer.accepted is False


def test_picker_pins_the_up_key_filter_mode_to_the_session(fake_atuin):
    """A user's filter_mode_shell_up_key_binding beats --filter-mode, and would offer shell commands."""
    fake = fake_atuin(stderr='')
    search_history(FakeEvent(FakeBuffer()), 'mycli', up_key_binding=True)
    assert fake.env['ATUIN_FILTER_MODE_SHELL_UP_KEY_BINDING'] == 'session'


def _up_bindings(config):
    from mycli.key_bindings import mycli_bindings

    stub = type('Stub', (), {'config': config})()
    return [b for b in mycli_bindings(stub).bindings if any(str(k) == 'Keys.Up' for k in b.keys)]


def test_shipped_default_leaves_atuin_off():
    """The packaged myclirc must not turn the feature on for existing users."""
    from mycli.config import create_default_config

    c = create_default_config()
    assert c['main'].as_bool('atuin_history') is False
    assert c['main'].as_bool('atuin_keys') is False


def test_up_is_not_rebound_unless_enabled():
    assert _up_bindings({'main': {'atuin_history': 'False', 'atuin_keys': 'False'}}) == []


def test_up_is_rebound_when_enabled(monkeypatch):
    from mycli.packages.ptoolkit import atuin as atuin_module

    monkeypatch.setattr(atuin_module, 'is_available', lambda: True)
    assert len(_up_bindings({'main': {'atuin_history': 'True', 'atuin_keys': 'True'}})) == 1


def test_up_stays_with_an_active_search(monkeypatch):
    """Up during Ctrl-R or / must move through the search, not open atuin."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout import BufferControl, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import SearchBufferControl

    from mycli.packages.ptoolkit import atuin as atuin_module

    monkeypatch.setattr(atuin_module, 'is_available', lambda: True)
    (binding,) = _up_bindings({'main': {'atuin_history': 'True', 'atuin_keys': 'True'}})

    search_control = SearchBufferControl(Buffer())
    control = BufferControl(Buffer(), search_buffer_control=search_control)
    app = Application(layout=Layout(HSplit([Window(control), Window(search_control)]), focused_element=control))
    with set_app(app):
        assert binding.filter() is True
        app.layout.search_links[search_control] = control
        app.layout.focus(search_control)
        assert binding.filter() is False


def test_up_not_rebound_when_atuin_history_is_off(monkeypatch):
    """atuin's picker would show an empty list, so the key must stay on history."""
    from mycli.packages.ptoolkit import atuin as atuin_module

    monkeypatch.setattr(atuin_module, 'is_available', lambda: True)
    assert _up_bindings({'main': {'atuin_history': 'False', 'atuin_keys': 'True'}}) == []


def test_binding_construction_tolerates_a_config_without_main():
    """Bindings are built with stub configs in tests and other callers."""
    assert _up_bindings({}) == []


def test_timestamped_history_is_newest_first(fake_atuin, history):
    fake_atuin(stdout='', formatted='2026-01-01 00:00:00\told\x002026-01-02 00:00:00\tnew\x00')
    assert [command for command, _ in history().load_history_with_timestamp()] == ['new', 'old']


def test_finish_does_not_wait_for_history_end(fake_atuin, history):
    """`history end` syncs; waiting for it held up the prompt and let Ctrl-C quit mycli."""
    fake = fake_atuin(stdout='id\n')
    h = history()
    h.store_string('SELECT 1')
    h.finish(0)

    assert fake.calls[-1][:3] == ['atuin', 'history', 'end']
    assert fake.popen_kwargs == {
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
        'start_new_session': True,
    }


def test_finish_survives_a_missing_executable(fake_atuin, history, monkeypatch):
    fake_atuin(stdout='id\n')
    h = history()
    h.store_string('SELECT 1')

    def boom(argv, **kwargs):
        raise FileNotFoundError('atuin')

    monkeypatch.setattr(subprocess, 'Popen', boom)
    h.finish(0)  # must not raise


@pytest.mark.parametrize(
    'failure',
    [
        {'returncode': 1, 'stderr': 'database is locked'},
        {'raises': subprocess.TimeoutExpired('atuin', 5)},
        {'raises': PermissionError('atuin')},
        {'stdout': '\n'},
    ],
)
def test_a_failed_start_stores_the_query_in_the_file_and_warns_once(fake_atuin, history, tmp_path, failure):
    fake_atuin(**failure)
    h = history()
    h.store_string('SELECT 1')
    h.store_string('SELECT 2')

    text = (tmp_path / 'hist').read_text(encoding='utf-8')
    assert '+SELECT 1\n' in text and '+SELECT 2\n' in text
    warning = h.take_warning()
    assert warning is not None and str(tmp_path / 'hist') in warning
    assert '\n' not in warning
    assert h.take_warning() is None


def test_a_working_start_writes_nothing_to_the_file(fake_atuin, history, tmp_path):
    fake_atuin(stdout='id\n')
    h = history()
    h.store_string('SELECT 1')
    assert not (tmp_path / 'hist').exists()
    assert h.take_warning() is None


SECRET_LINES = [
    "CREATE USER 'bob'@'%' IDENTIFIED BY 'Secret1!'",
    "create user if not exists bob identified by 'Secret1!'",
    "GRANT ALL ON *.* TO bob IDENTIFIED BY 'Secret1!'",
    "ALTER USER bob IDENTIFIED WITH caching_sha2_password BY 'Secret1!'",
    "CREATE USER bob IDENTIFIED WITH mysql_native_password AS '*2470C0C06DEE42FD1618BB99005ADCA2EC9D1E19'",
    "SET PASSWORD = 'Secret1!'",
    "set  password  for bob = 'Secret1!'",
    "select 1; ALTER USER bob IDENTIFIED BY 'Secret1!'",
    "SELECT 1;\n  alter\tuser bob\n  identified by 'Secret1!';",
    "Alter User bob Identified By 'Secret1!'",
    "/*!80000 ALTER USER bob IDENTIFIED BY 'Secret1!' */",
    "CHANGE REPLICATION SOURCE TO SOURCE_USER='repl', SOURCE_PASSWORD='Secret1!'",
    "ALTER USER bob IDENTIFIED BY 'Secret1!",  # unterminated
]


@pytest.mark.parametrize('line', SECRET_LINES)
def test_lines_with_credentials_are_not_sent_to_atuin(fake_atuin, history, tmp_path, line):
    fake = fake_atuin(stdout='id\n')
    h = history()
    h.append_string(line)
    h.store_string(line)  # a direct caller is filtered too
    assert fake.calls == []
    assert not (tmp_path / 'hist').exists()


@pytest.mark.parametrize('line', SECRET_LINES)
def test_lines_with_credentials_are_not_written_to_the_history_file(tmp_path, line):
    h = FileHistoryWithTimestamp(tmp_path / 'hist', frecency_history_entries=0)
    h.append_string(line)
    assert not (tmp_path / 'hist').exists()


@pytest.mark.parametrize(
    'line',
    [
        'SELECT password FROM users',
        "SELECT * FROM t WHERE note = 'identified by password'",
        'SELECT `identified` FROM t',
        "UPDATE users SET password_hash = NULL WHERE id = 1",
        'SHOW VARIABLES LIKE "%password%"',
    ],
)
def test_lines_that_only_mention_passwords_are_still_stored(fake_atuin, history, tmp_path, line):
    fake = fake_atuin(stdout='id\n')
    history().append_string(line)
    assert fake.calls[-1][-1] == line

    h = FileHistoryWithTimestamp(tmp_path / 'file-hist', frecency_history_entries=0)
    h.append_string(line)
    assert f'+{line}\n' in (tmp_path / 'file-hist').read_text(encoding='utf-8')
