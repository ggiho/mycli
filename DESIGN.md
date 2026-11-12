# Design

## Source of truth
- Status: Active for the interactive favorite browser/editor; other mycli UI keeps its existing design.
- Refreshed: 2026-11-12.
- Evidence: `mycli/clistyle.py`, `favoritequeries.py`, `main_modes/repl.py`, user-approved favorite workflow and terminal feedback.

## Brand
- A quiet workspace that belongs in the user's WezTerm. Keep its default background and current SQL theme.
- Avoid opaque black panels, decorative badges and new global shortcuts.

## Product goals
- Find saved SQL without remembering a name; inspect it, load it for an ad hoc edit, or persist an intentional change.
- Success: selecting never runs SQL, saving is explicit, cancellation and storage failures preserve the original favorite.
- Non-goals: database execution/editing, tags and shared-file synchronization in this iteration.

## Personas and jobs
- Existing mycli user; mixed terminal experience; local and chezmoi-managed configuration.
- Jobs: search favorites, inspect full SQL, load into the normal prompt, edit and save under the same or another name.

## Information architecture
- Interactive bare `\f`: search first → names and SQL preview → parameter inputs if needed → filled SQL in prompt.
- `\fe [name]`: select if unnamed → SQL editor → Save / Save as / Cancel.
- `\f name` keeps direct execution. Script/source `\f` keeps its text list.

## Design principles
- Reuse prompt_toolkit, MyCliLexer, current styling and FavoriteQueries persistence.
- Full values remain available; names/SQL filtering does not mutate stored data.
- Separate temporary query changes from persistent favorite changes through explicit actions.

## Visual language
- Existing SQL syntax colors; terminal background; thin muted frame borders, bold titles and reverse selection/focus.
- Browser: search, names, full wrapped preview, status and actions. No extra images or animation.
- Editor: syntax-highlighted SQL with line numbers; explicit storage actions below it.

## Components
- prompt_toolkit TextArea, Frame, Button and focus traversal; a selectable formatted-text name list.
- No dependencies or additional theme system.

## Accessibility
- Search receives initial focus; Enter selects immediately. Escape enters list navigation.
- In navigation, j/k select or read preview lines; h/l focus list/preview.
- f or / reopens search; Escape returns to list navigation with the filter retained.
- Search/editor/name fields accept hjkl and search characters as text; Tab traversal skips inactive search.
- Arrows remain available; Enter on either pane loads/opens editing; Tab/Shift+Tab reach actions.
- Buttons and selection markers convey state independently of color. Mouse selection is supported.
- SQL editor follows configured Emacs/Vi mode; Escape retains its Vim meaning in the Vi editor.
- Keep focus visible. No screen-reader certification is claimed.

## Responsive behavior
- At 80 columns and wider, place names beside preview; below 80 stack a bounded list above preview.
- Wrap SQL rather than truncate it. Keep the editor/actions usable in ordinary narrow terminals.

## Interaction states
- Empty: inline instructions for saving a favorite. No matches: explicit message and empty preview.
- Error: keep edited SQL available and show a storage error; no success result or alias refresh.
- Save as collision: explicit Replace/Back choice. Dirty cancel: Discard/Keep editing choice.
- Saved: return a status and refresh local aliases without database completion queries.
- Browser/editor never acquires a database connection. Load asks for static named/numbered values; editor keeps the template raw.
- Parameter forms use native template defaults for omitted named values. Errors retain fields; Back preserves favorites.

## Content voice
- Short English action labels consistent with mycli: Search, SQL preview, Load, Edit, Save, Save as, Cancel.

## Implementation constraints
- Lazy-load the UI so normal startup and ordinary SQL gain no widget initialization.
- Persist through FavoriteQueries.save using a staged write and atomic replacement; preserve file permissions and managed symlinks.
- never publish user favorites or write their values to chezmoi automatically.
- Test with temporary configurations and synthetic SQL, including template text, write failures and cancellation.
- Verify real terminal restoration and the interactive/noninteractive boundary.

## Open questions
- None blocking this iteration. Shared-query provenance can be considered separately.
