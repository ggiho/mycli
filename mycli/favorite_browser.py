"""Interactive favorite selection and editing, loaded only when requested."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import Dimension, DynamicContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import AnyContainer, ScrollOffsets
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.styles import BaseStyle, Style, merge_styles
from prompt_toolkit.widgets import Button, Frame, Label, TextArea

from mycli.lexer import MyCliLexer
from mycli.packages.special.favoritequeries import FavoriteQueries


@dataclass
class FavoriteOutcome:
    query: str | None = None
    message: str = ''
    saved: bool = False


class FavoriteBrowser:
    def __init__(
        self,
        favorites: FavoriteQueries,
        *,
        style: BaseStyle | None = None,
        editing_mode: EditingMode = EditingMode.EMACS,
        edit: bool = False,
        name: str = '',
        input_stream: Input | None = None,
        output_stream: Output | None = None,
    ) -> None:
        self.favorites = favorites
        self.editing_mode = editing_mode
        self.edit_on_enter = edit
        self.entries = [(n, favorites.get(n) or '') for n in favorites.list() if n]
        self.filtered = self.entries[:]
        self.index = 0
        self.screen = 'browse'
        self.notice = ''
        self.original_name = ''
        self.original_sql = ''
        self.pending_name = ''
        self.search = TextArea(
            height=1,
            multiline=False,
            prompt=lambda: 'Search: ' if self._searching() else 'Search (f,/): ',
            name='favorite-search',
        )
        self.preview = TextArea(read_only=True, lexer=PygmentsLexer(MyCliLexer), scrollbar=True, focus_on_click=True)
        self.editor = TextArea(lexer=PygmentsLexer(MyCliLexer), line_numbers=True, scrollbar=True, focus_on_click=True)
        self.new_name = TextArea(height=1, multiline=False, prompt='New name: ', name='favorite-name')
        self.new_name.buffer.accept_handler = self._accept_name
        self.list_control = FormattedTextControl(
            self._list_text, focusable=True, show_cursor=False, get_cursor_position=lambda: Point(0, self.index)
        )
        self.list_window = Window(self.list_control, scroll_offsets=ScrollOffsets(top=2, bottom=2))
        self.load_button = Button('Load', self.load)
        self.edit_button = Button('Edit', self.edit_selected)
        self.cancel_button = Button('Cancel', self.cancel)
        self.save_button = Button('Save', self.save)
        self.save_as_button = Button('Save as…', self.save_as)
        self.back_button = Button('Back', self.back_to_editor)
        self.named_save_button = Button('Save', self.save_named)
        self.replace_button = Button('Replace', self.replace)
        self.discard_button = Button('Discard', lambda: self.application.exit())
        self.continue_button = Button('Keep editing', self.back_to_editor, width=16)
        self.application: Application[FavoriteOutcome | None] = Application(
            layout=Layout(DynamicContainer(self._body), focused_element=self.list_control),
            key_bindings=self._bindings(),
            style=merge_styles([
                style or Style([]),
                Style.from_dict({
                    '': 'bg:default',
                    'frame.border': 'fg:ansibrightblack',
                    'frame.label': 'bold',
                    'button': 'bg:default',
                    'button.focused': 'reverse',
                    'favorite.selected': 'reverse',
                }),
            ]),
            full_screen=True,
            mouse_support=True,
            input=input_stream,
            output=output_stream,
        )
        self.search.buffer.on_text_changed += self._filter
        self._filter()
        if name:
            self.start_edit(name)

    def _list_text(self) -> StyleAndTextTuples:
        if not self.filtered:
            return [('', ' No matches')]
        return [
            ('class:favorite.selected' if i == self.index else '', f' {"›" if i == self.index else " "} {name}\n', partial(self._mouse, i))
            for i, (name, _sql) in enumerate(self.filtered)
        ]

    def _mouse(self, index: int, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_UP:
            self.index = index
            self.application.layout.focus(self.list_control)
            self._update_preview()
        elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.move(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)

    def _filter(self, *_args: object) -> None:
        words = self.search.text.casefold().split()
        self.filtered = [(n, q) for n, q in self.entries if all(w in f'{n}\n{q}'.casefold() for w in words)]
        self.index = 0
        self.notice = ''
        self._update_preview()

    def _update_preview(self) -> None:
        sql = self.filtered[self.index][1] if self.filtered else ''
        self.preview.buffer.set_document(Document(sql, cursor_position=0), bypass_readonly=True)
        self.application.invalidate()

    def move(self, step: int) -> None:
        self.index = max(0, min(self.index + step, len(self.filtered) - 1))
        self._update_preview()

    def _searching(self) -> bool:
        return self.screen == 'browse' and hasattr(self, 'application') and self.application.layout.has_focus(self.search)

    def begin_search(self) -> None:
        self.application.layout.focus(self.search)
        self.search.buffer.cursor_position = len(self.search.text)

    def finish_search(self) -> None:
        self.application.layout.focus(self.list_control)

    def _move_vertical(self, step: int) -> None:
        if self.application.layout.has_focus(self.preview):
            if step > 0:
                self.preview.buffer.cursor_down(count=step)
            else:
                self.preview.buffer.cursor_up(count=-step)
        else:
            self.move(step)
            self.application.layout.focus(self.list_control)

    def _buttons(self, *buttons: Button) -> VSplit:
        return VSplit(list(buttons), padding=2)

    def _body(self) -> HSplit:
        status = Label(lambda: self.notice, style='class:output.status')
        if self.screen == 'browse':
            wide = get_app().output.get_size().columns >= 80
            listing = Frame(
                self.list_window,
                title=f'Favorites · {len(self.filtered)}/{len(self.entries)}',
                width=Dimension(weight=1) if wide else None,
                height=None if wide else Dimension(max=8),
            )
            selected = self.filtered[self.index][0] if self.filtered else '—'
            preview = Frame(self.preview, title=f'SQL preview · {selected}', width=Dimension(weight=2) if wide else None)
            body: VSplit | HSplit
            if wide:
                body = VSplit([listing, preview], padding=1)
            else:
                body = HSplit([listing, preview])
            action = 'edit' if self.edit_on_enter else 'load'
            return HSplit([
                self.search,
                body,
                status,
                Label(
                    'Search · Enter/Esc list · ↑↓ select'
                    if self._searching()
                    else f'hjkl move · f,/ search · Enter {action} · Tab actions · Esc close'
                ),
                self._buttons(self.load_button, self.edit_button, self.cancel_button),
            ])
        footer: list[AnyContainer]
        if self.screen == 'save_as':
            footer = [self.new_name, status, self._buttons(self.named_save_button, self.back_button)]
        elif self.screen == 'overwrite':
            footer = [Label(f'"{self.pending_name}" already exists. Replace it?'), self._buttons(self.replace_button, self.back_button)]
        elif self.screen == 'discard':
            footer = [Label('Discard unsaved changes?'), self._buttons(self.discard_button, self.continue_button)]
        else:
            footer = [
                status,
                Label('Tab actions · Save updates the favorite · Cancel returns without saving'),
                self._buttons(self.save_button, self.save_as_button, self.cancel_button),
            ]
        return HSplit([Frame(self.editor, title=f'Edit favorite · {self.original_name}'), *footer])

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()
        browsing = Condition(
            lambda: (
                self.screen == 'browse'
                and self.application.layout.has_focus(self.search)
                or self.screen == 'browse'
                and self.application.layout.has_focus(self.list_control)
            )
        )
        navigation = Condition(lambda: self.screen == 'browse' and not self._searching())

        @kb.add('j', filter=navigation)
        def vim_down(_event: KeyPressEvent) -> None:
            self._move_vertical(1)

        @kb.add('k', filter=navigation)
        def vim_up(_event: KeyPressEvent) -> None:
            self._move_vertical(-1)

        @kb.add('h', filter=navigation)
        def focus_list(_event: KeyPressEvent) -> None:
            self.application.layout.focus(self.list_control)

        @kb.add('l', filter=navigation)
        def focus_preview(_event: KeyPressEvent) -> None:
            self.application.layout.focus(self.preview)

        @kb.add('f', filter=navigation)
        @kb.add('/', filter=navigation)
        def search(_event: KeyPressEvent) -> None:
            self.begin_search()

        @kb.add('up', filter=browsing)
        def up(_event: KeyPressEvent) -> None:
            self.move(-1)

        @kb.add('down', filter=browsing)
        def down(_event: KeyPressEvent) -> None:
            self.move(1)

        @kb.add('enter', filter=browsing | Condition(lambda: self.screen == 'browse' and self.application.layout.has_focus(self.preview)))
        def select(_event: KeyPressEvent) -> None:
            if self._searching():
                self.finish_search()
            elif self.edit_on_enter:
                self.edit_selected()
            else:
                self.load()

        @kb.add('tab')
        def next_field(event: KeyPressEvent) -> None:
            focus_next(event)
            if self._searching():
                focus_next(event)

        @kb.add('s-tab')
        def previous_field(event: KeyPressEvent) -> None:
            focus_previous(event)
            if self._searching():
                focus_previous(event)

        @kb.add('escape', filter=Condition(lambda: self.screen != 'edit' or self.editing_mode != EditingMode.VI))
        def escape(_event: KeyPressEvent) -> None:
            if self._searching():
                self.finish_search()
            elif self.screen in ('save_as', 'overwrite', 'discard'):
                self.back_to_editor()
            else:
                self.cancel()

        @kb.add('c-c')
        def interrupt(_event: KeyPressEvent) -> None:
            self.cancel()

        return kb

    def load(self) -> None:
        if not self.filtered:
            return
        query = self.filtered[self.index][1]
        if not query.strip():
            self.notice = 'This favorite is empty. Choose Edit to add SQL.'
            return
        self.application.exit(result=FavoriteOutcome(query=query))

    def edit_selected(self) -> None:
        if self.filtered:
            self.start_edit(self.filtered[self.index][0])

    def start_edit(self, name: str) -> None:
        self.notice = ''
        self.original_name = name
        self.original_sql = self.favorites.get(name) or ''
        self.editor.text = self.original_sql
        self.back_to_editor()

    def back_to_editor(self) -> None:
        self.screen = 'edit'
        self.application.editing_mode = self.editing_mode
        self.application.vi_state.input_mode = InputMode.INSERT
        self.application.layout.focus(self.editor)
        self.application.invalidate()

    def save_as(self) -> None:
        self.new_name.text = ''
        self.screen = 'save_as'
        self.notice = ''
        self.application.editing_mode = EditingMode.EMACS
        self.application.layout.focus(self.new_name)

    def save_named(self) -> None:
        name = self.new_name.text.strip()
        if not name or any(c.isspace() or not c.isprintable() or c in '[]=;' for c in name):
            self.notice = 'Use one name without spaces, brackets, = or ;.'
            return
        self.notice = ''
        if self.favorites.get(name) is not None:
            self.pending_name = name
            self.screen = 'overwrite'
            self.application.layout.focus(self.replace_button)
        else:
            self._save(name)

    def _accept_name(self, _buffer: object) -> bool:
        self.save_named()
        return True

    def save(self) -> None:
        self._save(self.original_name)

    def replace(self) -> None:
        self._save(self.pending_name)

    def _save(self, name: str) -> None:
        if not self.editor.text.strip():
            self.notice = 'Empty query: nothing saved.'
            self.back_to_editor()
            return
        if name == self.original_name and self.editor.text == self.original_sql:
            self.application.exit(result=FavoriteOutcome(message=f'{name}: No changes.'))
            return
        try:
            self.favorites.save(name, self.editor.text)
        except Exception as error:
            self.notice = f'Could not save: {error}'
            self.back_to_editor()
            return
        self.application.exit(result=FavoriteOutcome(message=f'{name}: Saved.', saved=True))

    def cancel(self) -> None:
        if self.screen != 'browse' and self.editor.text != self.original_sql:
            self.screen = 'discard'
            self.application.layout.focus(self.continue_button)
        else:
            self.application.exit()


def browse_favorites(
    favorites: FavoriteQueries,
    *,
    style: BaseStyle | None = None,
    editing_mode: EditingMode = EditingMode.EMACS,
    edit: bool = False,
    name: str = '',
) -> FavoriteOutcome | None:
    if name and favorites.get(name) is None:
        return FavoriteOutcome(message=f'No favorite query: {name}')
    if not favorites.list():
        return FavoriteOutcome(message=r'No favorite queries. Save one with \fs name SQL.')
    return FavoriteBrowser(favorites, style=style, editing_mode=editing_mode, edit=edit, name=name).application.run()
