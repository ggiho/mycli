from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

from configobj import ConfigObj
from prompt_toolkit.data_structures import Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.output import DummyOutput
import pytest

from mycli.favorite_browser import FavoriteBrowser, FavoriteOutcome, browse_favorites
from mycli.packages.special.favoritequeries import FavoriteQueries


@pytest.fixture
def favorites(tmp_path: Path) -> FavoriteQueries:
    path = tmp_path / 'myclirc'
    config = ConfigObj(encoding='utf-8')
    config.filename = str(path)
    config['main'] = {'unrelated': 'keep this'}
    config['favorite_queries'] = {
        'all_users': 'SELECT * FROM users',
        'orders': "SELECT * FROM orders WHERE city = 'Busan'",
        'template': "SELECT $1, '{{ kv.name }}'",
    }
    config.write()
    return FavoriteQueries(config, str(path))


@pytest.fixture
def browser(favorites: FavoriteQueries, monkeypatch: pytest.MonkeyPatch) -> tuple[FavoriteBrowser, Mock]:
    ui = FavoriteBrowser(favorites, input_stream=DummyInput(), output_stream=DummyOutput())
    exit_app = Mock()
    monkeypatch.setattr(ui.application, 'exit', exit_app)
    return ui, exit_app


def test_searches_names_and_sql_and_previews_full_query(browser: tuple[FavoriteBrowser, Mock]) -> None:
    ui, _ = browser
    ui.search.text = 'BUSAN'
    assert [n for n, _ in ui.filtered] == ['orders']
    assert ui.preview.text == "SELECT * FROM orders WHERE city = 'Busan'"
    ui.search.text = 'no_such_favorite'
    assert ui.filtered == [] and ui.preview.text == ''
    ui.load()
    ui.search.text = ''
    ui.move(100)
    assert ui.preview.text == "SELECT $1, '{{ kv.name }}'"


def test_load_preserves_raw_template_without_saving_or_executing(browser: tuple[FavoriteBrowser, Mock]) -> None:
    ui, exit_app = browser
    ui.search.text = 'template'
    before = Path(ui.favorites.config_file or '').read_bytes()
    ui.load()
    outcome = exit_app.call_args.kwargs['result']
    assert outcome == FavoriteOutcome(query="SELECT $1, '{{ kv.name }}'")
    assert Path(ui.favorites.config_file or '').read_bytes() == before


def test_save_updates_original_and_preserves_unrelated_config(browser: tuple[FavoriteBrowser, Mock]) -> None:
    ui, exit_app = browser
    ui.start_edit('orders')
    ui.editor.text = 'SELECT *\nFROM orders\nWHERE id = 23;'
    ui.save()
    stored = ConfigObj(ui.favorites.config_file, encoding='utf-8')
    assert stored['favorite_queries']['orders'] == 'SELECT *\nFROM orders\nWHERE id = 23'
    assert stored['main']['unrelated'] == 'keep this'
    assert exit_app.call_args.kwargs['result'].saved


def test_save_as_creates_a_copy_and_keeps_original(browser: tuple[FavoriteBrowser, Mock]) -> None:
    ui, exit_app = browser
    original = ui.favorites.get('orders')
    ui.start_edit('orders')
    ui.editor.text = 'SELECT 23'
    ui.save_as()
    ui.new_name.text = 'orders_copy'
    ui.save_named()
    assert ui.favorites.get('orders') == original
    assert ui.favorites.get('orders_copy') == 'SELECT 23'
    assert exit_app.call_args.kwargs['result'].message == 'orders_copy: Saved.'


def test_existing_save_as_name_requires_explicit_replace(browser: tuple[FavoriteBrowser, Mock]) -> None:
    ui, exit_app = browser
    ui.start_edit('orders')
    ui.editor.text = 'SELECT 23'
    ui.save_as()
    ui.new_name.text = 'all_users'
    ui.save_named()
    assert ui.screen == 'overwrite'
    assert ui.favorites.get('all_users') == 'SELECT * FROM users'
    exit_app.assert_not_called()
    ui.replace()
    assert ui.favorites.get('all_users') == 'SELECT 23'


@pytest.mark.parametrize('name', ['', 'a b', '[section]', 'x=y', 'a;SELECT', 'x\x1b'])
def test_invalid_copy_names_never_write(browser: tuple[FavoriteBrowser, Mock], name: str) -> None:
    ui, exit_app = browser
    before = Path(ui.favorites.config_file or '').read_bytes()
    ui.start_edit('orders')
    ui.save_as()
    ui.new_name.text = name
    ui.save_named()
    assert ui.notice and Path(ui.favorites.config_file or '').read_bytes() == before
    exit_app.assert_not_called()


def test_dirty_cancel_keeps_edits_until_discarded(browser: tuple[FavoriteBrowser, Mock]) -> None:
    ui, exit_app = browser
    original = ui.favorites.get('orders')
    ui.start_edit('orders')
    ui.editor.text = 'SELECT 23'
    ui.cancel()
    assert ui.screen == 'discard'
    exit_app.assert_not_called()
    ui.back_to_editor()
    assert ui.editor.text == 'SELECT 23'
    ui.discard_button.handler()
    exit_app.assert_called_once_with()
    assert ui.favorites.get('orders') == original


def test_write_failure_keeps_editor_text_and_original_file(browser: tuple[FavoriteBrowser, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    ui, exit_app = browser
    before = Path(ui.favorites.config_file or '').read_bytes()
    ui.start_edit('orders')
    ui.editor.text = 'SELECT 23'
    monkeypatch.setattr(ui.favorites, 'save', Mock(side_effect=OSError('disk full')))
    ui.save()
    assert ui.screen == 'edit' and ui.editor.text == 'SELECT 23'
    assert 'disk full' in ui.notice and Path(ui.favorites.config_file or '').read_bytes() == before
    exit_app.assert_not_called()


def test_empty_query_is_not_saved(browser: tuple[FavoriteBrowser, Mock]) -> None:
    ui, exit_app = browser
    ui.start_edit('orders')
    ui.editor.text = ' \n '
    ui.save()
    assert 'Empty query' in ui.notice
    assert ui.favorites.get('orders') == "SELECT * FROM orders WHERE city = 'Busan'"
    exit_app.assert_not_called()


def test_missing_and_empty_favorites_return_inline(favorites: FavoriteQueries) -> None:
    missing = browse_favorites(favorites, edit=True, name='missing')
    assert missing is not None and missing.message == 'No favorite query: missing'
    empty = browse_favorites(FavoriteQueries(ConfigObj()))
    assert empty is not None and 'No favorite queries' in empty.message


@pytest.mark.parametrize('columns', [40, 80, 140])
def test_real_application_search_and_enter_only_load(favorites: FavoriteQueries, columns: int) -> None:
    class Output(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=24, columns=columns)

    async def scenario() -> FavoriteOutcome | None:
        with create_pipe_input() as pipe:
            ui = FavoriteBrowser(favorites, input_stream=pipe, output_stream=Output())
            task = asyncio.create_task(ui.application.run_async())
            await asyncio.sleep(0)
            pipe.send_text('orders\r')
            return await asyncio.wait_for(task, 3)

    outcome = asyncio.run(scenario())
    assert outcome is not None and outcome.query == "SELECT * FROM orders WHERE city = 'Busan'"
    assert not outcome.saved


def test_real_vi_editor_escape_keeps_vim_navigation(favorites: FavoriteQueries) -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            ui = FavoriteBrowser(favorites, name='orders', editing_mode=EditingMode.VI, input_stream=pipe, output_stream=DummyOutput())
            ui.application.ttimeoutlen = 0.01
            ui.application.timeoutlen = 0.01
            task = asyncio.create_task(ui.application.run_async())
            await asyncio.sleep(0)
            pipe.send_text('\x1b')
            for _ in range(100):
                if ui.application.vi_state.input_mode == InputMode.NAVIGATION:
                    break
                await asyncio.sleep(0.01)
            assert ui.application.vi_state.input_mode == InputMode.NAVIGATION
            assert not task.done()
            ui.cancel()
            assert await asyncio.wait_for(task, 3) is None

    asyncio.run(scenario())


def test_shared_favorite_save_creates_local_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = tmp_path / 'user-config'
    shared = tmp_path / 'shared-config'
    user.write_text('[main]\nunrelated = keep\n', encoding='utf-8')
    shared.write_text('[favorite_queries]\nreport = SELECT 1\n', encoding='utf-8')
    before = shared.read_bytes()
    favorites = FavoriteQueries.from_config(ConfigObj(str(user)), str(user), str(shared))
    ui = FavoriteBrowser(favorites, name='report', input_stream=DummyInput(), output_stream=DummyOutput())
    monkeypatch.setattr(ui.application, 'exit', Mock())
    ui.editor.text = 'SELECT 23'
    ui.save()
    assert shared.read_bytes() == before
    assert ConfigObj(str(user))['favorite_queries']['report'] == 'SELECT 23'


def test_normal_repl_import_does_not_load_browser_widgets() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, '-c', "import mycli.main_modes.repl,sys; assert 'mycli.favorite_browser' not in sys.modules"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_unchanged_save_does_not_rewrite_configuration(browser: tuple[FavoriteBrowser, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    ui, exit_app = browser
    ui.start_edit('orders')
    save = Mock()
    monkeypatch.setattr(ui.favorites, 'save', save)
    ui.save()
    save.assert_not_called()
    result = exit_app.call_args.kwargs['result']
    assert result.message == 'orders: No changes.' and not result.saved
