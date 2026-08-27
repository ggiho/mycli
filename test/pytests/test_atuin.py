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


@pytest.fixture
def fake_atuin(monkeypatch):
    def install(**kwargs):
        fake = FakeAtuin(**kwargs)
        monkeypatch.setattr(subprocess, 'run', fake)
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
    fake = fake_atuin(stdout='SELECT 2\0SELECT 1\0SELECT 1\0')
    assert list(history().load_history_strings()) == ['SELECT 2', 'SELECT 1', 'SELECT 1']
    argv = fake.calls[0]
    assert argv[:2] == ['atuin', 'search']
    assert argv[argv.index('--author') + 1] == 'mycli'
    # Duplicates feed the frecency score.
    assert '--include-duplicates' in argv
    # atuin prints oldest first; without --reverse the up-arrow order inverts.
    assert '--reverse' in argv
    assert '--cmd-only' in argv


def test_load_preserves_multiline_entries(fake_atuin, history):
    fake_atuin(stdout='SELECT a,\n       b\n  FROM t\0SELECT 1\0')
    assert list(history().load_history_strings()) == ['SELECT a,\n       b\n  FROM t', 'SELECT 1']


def test_history_file_is_appended_as_the_tail(fake_atuin, history, tmp_path):
    (tmp_path / 'hist').write_text('\n# 2020-01-01 00:00:00\n+old query\n', encoding='utf-8')
    fake_atuin(stdout='new query\0')
    assert list(history().load_history_strings()) == ['new query', 'old query']


def test_falls_back_to_the_file_when_atuin_fails(fake_atuin, history, tmp_path):
    (tmp_path / 'hist').write_text('\n# 2020-01-01 00:00:00\n+old query\n', encoding='utf-8')
    fake_atuin(returncode=1)
    assert list(history().load_history_strings()) == ['old query']


def test_store_tags_author_and_session_without_a_fake_exit(fake_atuin, history):
    fake = fake_atuin(stdout='01a040f0cb257242b718f2ab411b84ce\n')
    history(author='mycli-test').store_string('SELECT 1')
    assert fake.calls == [['atuin', 'history', 'start', '--author', 'mycli-test', 'SELECT 1']]
    # `history end` needs an exit code we cannot know at store time.
    assert not any('end' in call for call in fake.calls)
    assert fake.env['ATUIN_SESSION'] == session_id('mycli-test')


def test_password_changes_are_not_sent_to_atuin(fake_atuin, history):
    """append_string() inherits upstream's is_password_change filter."""
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


def test_survives_a_missing_or_wedged_executable(fake_atuin, history):
    for boom in (FileNotFoundError('atuin'), subprocess.TimeoutExpired('atuin', 5)):
        fake_atuin(raises=boom)
        h = history()
        assert list(h.load_history_strings()) == []
        h.store_string('SELECT 1')  # must not raise


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


def test_up_not_rebound_when_atuin_history_is_off(monkeypatch):
    """atuin's picker would show an empty list, so the key must stay on history."""
    from mycli.packages.ptoolkit import atuin as atuin_module

    monkeypatch.setattr(atuin_module, 'is_available', lambda: True)
    assert _up_bindings({'main': {'atuin_history': 'False', 'atuin_keys': 'True'}}) == []


def test_binding_construction_tolerates_a_config_without_main():
    """Bindings are built with stub configs in tests and other callers."""
    assert _up_bindings({}) == []
