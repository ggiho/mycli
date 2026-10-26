from functools import partial
import logging
import re
import webbrowser

import click
import prompt_toolkit
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import get_app
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import (
    Condition,
    completion_is_selected,
    control_is_searchable,
    emacs_mode,
    is_searching,
    shift_selection_mode,
    vi_mode,
)
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.named_commands import register as ptoolkit_register
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.selection import SelectionType
import sqlparse

from mycli.constants import DOCS_URL
from mycli.packages import key_binding_utils, special
from mycli.packages.ptoolkit import atuin
from mycli.packages.ptoolkit.fzf import search_history
from mycli.packages.ptoolkit.utils import safe_invalidate_display
from mycli.packages.whitespace import strip_invisible_outside_literals

_logger = logging.getLogger(__name__)


def normalize_pasted_text(data: str) -> str:
    """Turn a clipboard payload into text MySQL can parse.

    Only invisible characters outside quoted literals are touched; nothing
    visible is rewritten.
    """
    # prompt_toolkit's own paste handler does this, and overriding it drops it.
    data = data.replace('\r\n', '\n').replace('\r', '\n')
    return strip_invisible_outside_literals(data)


@Condition
def ctrl_d_condition() -> bool:
    """Ctrl-D exit binding is only active when the buffer is empty."""
    app = get_app()
    return not app.current_buffer.text


@Condition
def in_completion() -> bool:
    app = get_app()
    return bool(app.current_buffer.complete_state)


def print_f1_help():
    app = get_app()
    app.print_text('\n')
    app.print_text([
        ('', 'Inline help — type "'),
        ('bold', 'help'),
        ('', '" or "'),
        ('bold', r'\?'),
        ('', '"\n'),
    ])
    app.print_text([
        ('', 'Docs index — '),
        ('bold', DOCS_URL),
        ('', '\n'),
    ])
    app.print_text('\n')


@ptoolkit_register("edit-and-execute-command")
def edit_and_execute(event: KeyPressEvent) -> None:
    """Different from the prompt-toolkit default, we want to have a choice not
    to execute a query after editing, hence validate_and_handle=False."""
    buff = event.current_buffer
    buff.open_in_editor(validate_and_handle=False)


# Statements EXPLAIN accepts that it only plans and never runs. EXPLAIN ANALYZE
# does run the statement, and is excluded by not allowing a leading EXPLAIN.
EXPLAINABLE_KEYWORDS = frozenset({'select', 'insert', 'update', 'delete', 'replace', 'table', 'with'})


def explainable_statement(text: str, cursor_position: int) -> str | None:
    """Return the statement under the cursor if EXPLAIN can take it, else None.

    Comments are dropped (optimizer hints are kept), so what gets explained is
    exactly what was checked here.
    """
    statements = list(special.split_queries(text))
    if not statements:
        return None
    # Each statement that ends before the cursor shifts it one along.
    index = max(len(list(special.split_queries(text[:cursor_position]))) - 1, 0)
    sql = statements[min(index, len(statements) - 1)]
    sql = sqlparse.format(sql, strip_comments=True).strip().rstrip(';').strip()
    sql = re.sub(r'\\[Ggx]$', '', sql).strip()
    # With a custom delimiter one "statement" can still hold a `;`.
    if len(sqlparse.split(sql)) != 1:
        return None
    tokens = sqlparse.parse(sql)[0].flatten()
    first = next((t.value.lower() for t in tokens if not t.is_whitespace and t.ttype not in sqlparse.tokens.Comment), '')
    return sql if first in EXPLAINABLE_KEYWORDS else None


def _run_explain(mycli, sql: str) -> None:
    """Run EXPLAIN for one statement and print a small preview.

    Called through run_in_terminal, so it runs on the main thread with the
    prompt out of the way. The statement goes straight to a cursor: never
    through SQLExecute.run(), which would split it and dispatch special commands.
    """
    try:
        with mycli.sqlexecute.conn.cursor() as cur:
            cur.execute(f'EXPLAIN {sql}')
            header = [column[0] for column in cur.description or []]
            rows = [tuple('NULL' if v is None else str(v) for v in row) for row in cur.fetchall()]
    except Exception as e:
        _logger.debug('EXPLAIN preview failed: %r', e)
        return
    if not header or not rows:
        return
    widths = [len(h) for h in header]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))
    click.echo('\n--- EXPLAIN Preview (F5) ---')
    click.echo(' | '.join(h.ljust(widths[i]) for i, h in enumerate(header)))
    click.echo('-+-'.join('-' * w for w in widths))
    for row in rows:
        click.echo(' | '.join(v.ljust(widths[i]) for i, v in enumerate(row)))
    click.echo('---')


def mycli_bindings(mycli) -> KeyBindings:
    """Custom key bindings for mycli."""
    kb = KeyBindings()

    def _atuin_setting(key: str, default: str = '') -> str:
        """Read a [main] value without assuming the section exists.

        This runs while the bindings are built, and callers may pass a stub
        config, so it must not index into missing sections.
        """
        config = getattr(mycli, 'config', None)
        config_getter = getattr(config, 'get', None)
        main = config_getter('main', {}) if config_getter else {}
        getter = getattr(main, 'get', None)
        return str(getter(key, default) if getter else default)

    def _atuin_keys_enabled() -> bool:
        """atuin's picker can only search what mycli recorded there."""
        truthy = ('true', 'yes', 'on', '1')
        return _atuin_setting('atuin_keys').lower() in truthy and _atuin_setting('atuin_history').lower() in truthy and atuin.is_available()

    @kb.add('f5')
    def _(event: KeyPressEvent) -> None:
        """Run EXPLAIN on the statement under the cursor and show the plan."""
        _logger.debug('Detected F5 key.')
        buffer = event.app.current_buffer
        sql = explainable_statement(buffer.text, buffer.cursor_position)
        if sql is not None:
            run_in_terminal(partial(_run_explain, mycli, sql))

    # Registered only when enabled, so the default Up binding is untouched
    # otherwise. ~shift_selection_mode leaves shift-selection to prompt_toolkit,
    # and ~is_searching leaves Up to an active Ctrl-R or / search.
    if _atuin_keys_enabled():
        atuin_author = _atuin_setting('atuin_author', 'mycli')

        @kb.add('up', filter=~shift_selection_mode & ~is_searching)
        def _(event: KeyPressEvent) -> None:
            """Open atuin's history UI, mirroring `atuin init zsh`'s up-arrow widget."""
            buffer = event.current_buffer
            # atuin's widget only takes over for a single-line buffer; with a
            # completion menu open or multiple lines, Up must still move around.
            # This fallback is what prompt_toolkit's own "up" handler does.
            if buffer.complete_state or '\n' in buffer.text:
                buffer.auto_up(count=event.arg)
                return
            _logger.debug('Detected <up> key with atuin keys enabled.')
            if not atuin.search_history(event, atuin_author, up_key_binding=True):
                buffer.auto_up(count=event.arg)

    @kb.add('f1')
    def _(event: KeyPressEvent) -> None:
        """Open browser to documentation index."""
        _logger.debug('Detected F1 key.')
        webbrowser.open_new_tab(DOCS_URL)
        prompt_toolkit.application.run_in_terminal(print_f1_help)
        safe_invalidate_display(event.app)

    @kb.add('escape', '[', 'P')
    def _(event: KeyPressEvent) -> None:
        """Open browser to documentation index."""
        _logger.debug("Detected alternate F1 key sequence.")
        webbrowser.open_new_tab(DOCS_URL)
        prompt_toolkit.application.run_in_terminal(print_f1_help)
        safe_invalidate_display(event.app)

    @kb.add("f2")
    def _(_event: KeyPressEvent) -> None:
        """Enable/Disable SmartCompletion Mode."""
        _logger.debug("Detected F2 key.")
        mycli.completer.smart_completion = not mycli.completer.smart_completion

    @kb.add('escape', '[', 'Q')
    def _(_event: KeyPressEvent) -> None:
        """Enable/Disable SmartCompletion Mode."""
        _logger.debug("Detected alternate F2 key sequence.")
        mycli.completer.smart_completion = not mycli.completer.smart_completion

    @kb.add("f3")
    def _(_event: KeyPressEvent) -> None:
        """Enable/Disable Multiline Mode."""
        _logger.debug("Detected F3 key.")
        mycli.multi_line = not mycli.multi_line

    @kb.add('escape', '[', 'R')
    def _(_event: KeyPressEvent) -> None:
        """Enable/Disable Multiline Mode."""
        _logger.debug('Detected alternate F3 key sequence.')
        mycli.multi_line = not mycli.multi_line

    @kb.add("f4")
    def _(event: KeyPressEvent) -> None:
        """Toggle between Vi and Emacs mode."""
        _logger.debug("Detected F4 key.")
        if mycli.key_bindings == "vi":
            event.app.editing_mode = EditingMode.EMACS
            mycli.key_bindings = "emacs"
            event.app.ttimeoutlen = mycli.emacs_ttimeoutlen
        else:
            event.app.editing_mode = EditingMode.VI
            mycli.key_bindings = "vi"
            event.app.ttimeoutlen = mycli.vi_ttimeoutlen

    @kb.add('escape', '[', 'S')
    def _(event: KeyPressEvent) -> None:
        """Toggle between Vi and Emacs mode."""
        _logger.debug('Detected alternate F4 key sequence.')
        if mycli.key_bindings == 'vi':
            event.app.editing_mode = EditingMode.EMACS
            mycli.key_bindings = 'emacs'
            event.app.ttimeoutlen = mycli.emacs_ttimeoutlen
        else:
            event.app.editing_mode = EditingMode.VI
            mycli.key_bindings = 'vi'
            event.app.ttimeoutlen = mycli.vi_ttimeoutlen

    @kb.add("tab")
    def _(event: KeyPressEvent) -> None:
        """Complete action at cursor."""
        _logger.debug("Detected <Tab> key.")
        b = event.app.current_buffer

        behaviors = mycli.config['keys'].as_list('tab')

        if 'toolkit_default' in behaviors:
            if b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=True)

        if b.complete_state:
            if 'advance' in behaviors:
                b.complete_next()
            elif 'cancel' in behaviors:
                b.cancel_completion()
            return

        if 'advancing_summon' in behaviors:
            b.start_completion(select_first=True)
        elif 'prefixing_summon' in behaviors:
            b.start_completion(insert_common_part=True)
        elif 'summon' in behaviors:
            b.start_completion(select_first=False)

    @kb.add("escape", eager=vi_mode, filter=in_completion)
    def _(event: KeyPressEvent) -> None:
        """Cancel completion menu.

        There will be a lag when canceling Escape due to the processing of
        Alt- keystrokes as Escape- sequences.

        There will be no lag when using control-g to cancel."""
        event.app.current_buffer.cancel_completion()

    @kb.add("c-space")
    def _(event: KeyPressEvent) -> None:
        """
        Complete action at cursor.

        By default, if the autocompletion menu is not showing, display it with the
        appropriate completions for the context.

        If the menu is showing, select the next completion.
        """
        _logger.debug("Detected <C-Space> key.")

        b = event.app.current_buffer

        behaviors = mycli.config['keys'].as_list('control_space')

        if 'toolkit_default' in behaviors:
            if b.text:
                b.start_selection(selection_type=SelectionType.CHARACTERS)
            return

        if b.complete_state:
            if 'advance' in behaviors:
                b.complete_next()
            elif 'cancel' in behaviors:
                b.cancel_completion()
            return

        if 'advancing_summon' in behaviors:
            b.start_completion(select_first=True)
        elif 'prefixing_summon' in behaviors:
            b.start_completion(insert_common_part=True)
        elif 'summon' in behaviors:
            b.start_completion(select_first=False)

    @kb.add("c-x", "p", filter=emacs_mode)
    def _(event: KeyPressEvent) -> None:
        """
        Prettify and indent current statement, usually into multiple lines.

        Only accepts buffers containing single SQL statements.
        """
        _logger.debug("Detected <C-x p>/> key.")

        b = event.app.current_buffer
        if b.text:
            b.transform_region(0, len(b.text), partial(key_binding_utils.handle_prettify_binding, mycli))

    @kb.add("c-x", "u", filter=emacs_mode)
    def _(event: KeyPressEvent) -> None:
        """
        Unprettify and dedent current statement, usually into one line.

        Only accepts buffers containing single SQL statements.
        """
        _logger.debug("Detected <C-x u>/< key.")

        b = event.app.current_buffer
        if b.text:
            b.transform_region(0, len(b.text), partial(key_binding_utils.handle_unprettify_binding, mycli))

    @kb.add("c-o", "d", filter=emacs_mode)
    def _(event: KeyPressEvent) -> None:
        """
        Insert the current date.
        """
        _logger.debug("Detected <C-o d> key.")

        event.app.current_buffer.insert_text(key_binding_utils.server_date(mycli.sqlexecute))

    @kb.add("c-o", "c-d", filter=emacs_mode)
    def _(event: KeyPressEvent) -> None:
        """
        Insert the quoted current date.
        """
        _logger.debug("Detected <C-o C-d> key.")

        event.app.current_buffer.insert_text(key_binding_utils.server_date(mycli.sqlexecute, quoted=True))

    @kb.add("c-o", "t", filter=emacs_mode)
    def _(event: KeyPressEvent) -> None:
        """
        Insert the current datetime.
        """
        _logger.debug("Detected <C-o t> key.")

        event.app.current_buffer.insert_text(key_binding_utils.server_datetime(mycli.sqlexecute))

    @kb.add("c-o", "c-t", filter=emacs_mode)
    def _(event: KeyPressEvent) -> None:
        """
        Insert the quoted current datetime.
        """
        _logger.debug("Detected <C-o C-t> key.")

        event.app.current_buffer.insert_text(key_binding_utils.server_datetime(mycli.sqlexecute, quoted=True))

    @kb.add("c-r", filter=control_is_searchable)
    def _(event: KeyPressEvent) -> None:
        """Search history using fzf or reverse incremental search."""
        _logger.debug("Detected <C-r> key.")
        mode = mycli.config.get('keys', {}).get('control_r', 'auto')
        if mode == 'reverse_isearch':
            search_history(event, incremental=True)
        else:
            search_history(
                event,
                highlight_preview=mycli.highlight_preview,
                highlight_style=mycli.syntax_style,
            )

    @kb.add("escape", "r", filter=control_is_searchable & emacs_mode)
    def _(event: KeyPressEvent) -> None:
        """Search history using fzf when available."""
        _logger.debug("Detected <alt-r> key.")
        search_history(
            event,
            highlight_preview=mycli.highlight_preview,
            highlight_style=mycli.syntax_style,
        )

    @kb.add('c-d', filter=ctrl_d_condition)
    def _(event: KeyPressEvent) -> None:
        """Exit mycli or ignore keypress."""
        _logger.debug('Detected <C-d> key on empty line.')
        mode = mycli.config.get('keys', {}).get('control_d', 'exit')
        if mode == 'exit':
            event.app.exit(exception=EOFError, style='class:exiting')
        else:
            event.app.output.bell()

    @kb.add("enter", filter=completion_is_selected)
    def _(event: KeyPressEvent) -> None:
        """Makes the enter key work as the tab key only when showing the menu.

        In other words, don't execute query when enter is pressed in
        the completion dropdown menu, instead close the dropdown menu
        (accept current selection).

        """
        _logger.debug("Detected enter key.")

        event.current_buffer.complete_state = None
        b = event.app.current_buffer
        b.complete_state = None

    @kb.add("escape", "enter")
    def _(event: KeyPressEvent) -> None:
        """Introduces a line break in multi-line mode, or dispatches the
        command in single-line mode."""
        _logger.debug("Detected alt-enter key.")
        if mycli.multi_line:
            event.app.current_buffer.validate_and_handle()
        else:
            event.app.current_buffer.insert_text("\n")

    @kb.add(Keys.BracketedPaste)
    def _(event: KeyPressEvent) -> None:
        """Paste, dropping the invisible characters rich-text apps inject."""
        event.current_buffer.insert_text(normalize_pasted_text(event.data))

    return kb
