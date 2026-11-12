# Favorite query browser

In the interactive prompt, use `\f` (or `/f`) to search saved queries by name or
SQL text. Search receives initial focus: type a name or SQL text and press Enter
to select. `\f` loads the chosen query; `\fe` opens its template editor. A query
with parameters first asks for their values and then fills the normal query
prompt. Edit/review the resulting SQL there and press Enter when ready to execute
it. Loading never updates the stored favorite.

`\f name [args…]` keeps its existing direct execution behavior. In scripts and
source files, bare `\f` still prints the text list; `\favorite list` also prints
that list in the interactive prompt. Redirected, tee and logged output keeps the
existing text path.

Use `\fe` to select a favorite for editing, or `\fe name` to open it directly.
The **Edit** button in the browser opens the same editor.

- **Save** updates the original name. An unchanged query does not rewrite the file.
- **Save as…** asks for another name and keeps the original query. An existing
  destination requires explicit **Replace** confirmation.
- **Cancel** asks whether to discard unsaved SQL. **Keep editing** retains it.
- A storage error leaves the editor and its SQL available for another attempt.

Escape leaves search and retains its filter. In navigation mode, `j`/`k` select
favorites, `h`/`l` focus list/preview, and `j`/`k` move through SQL preview lines.
`f` or `/` reopens search. Search characters such as `hjkl`, `f` and `/` are inserted
normally while the search field is focused. Enter selects from either pane.
Tab/Shift+Tab traverse controls/actions; buttons also support the mouse.
The SQL editor follows the configured Emacs/Vi mode. In the Vi editor, Escape
keeps its normal Vim meaning; use **Cancel** to leave. In browser navigation,
Escape closes the browser. The browser adapts to narrow terminals and follows the current SQL
theme and terminal background. Modal screens use short Escape decoding/prefix
waits; the SQL editor keeps its normal multi-key editing timeout. Complete arrow
and Meta editing sequences still work. No terminal-global shortcut is installed.

For example, selecting a favorite named `vars` whose SQL is
`SHOW GLOBAL VARIABLES LIKE '%{{ kv.n }}%'` opens an `n` input. Enter `timeout`
and the query prompt receives `SHOW GLOBAL VARIABLES LIKE '%timeout%';`.

Named template keys (`kv.name`, `kv['name']`, `kv.get('name')`) and numbered
parameters (`$1`, `$2`, including repeated or multi-digit numbers) get individual
fields. Enter advances to the next field; on the last field it loads the filled
query. **Load** also submits the form. Escape/**Back** returns to the favorites
without loading or saving SQL. Back restores the pane and caret that opened the
form, so returning from search lets Backspace/Delete edit that search immediately.
Navigation/preview origins retain their focus. A render error keeps the fields
available to edit.

Blank named fields omit that key, preserving template defaults such as
`kv.get('n', 'default')`; without a default, ordinary `kv.n` expands to empty text.
Numbered inputs accept empty text explicitly. Inputs follow existing text-template
semantics, with surrounding SQL quotes still supplied by the stored template.
They are not database bind parameters. Input containing `$n` or Jinja-looking text
stays literal rather than being expanded a second time.

Dynamic templates such as `kv[kv.which]` also offer **Additional --key=value** for
keys that cannot be discovered statically. Use the existing argument quoting for
those extra values, e.g. `--name="Kim Min Su"`. The editor always edits the original
template, not the filled SQL. The existing `\f name value` and
`\favorite eval name value` commands keep their direct substitution behavior.

For a single-parameter `SHOW GLOBAL VARIABLES LIKE ...` favorite such as `vars`,
the parameter form also displays real variable names from the current connection
target. A compact template preview sits above the input, and the variable-name
list below it uses the remaining body space. Typing filters the names locally; Up/Down or a mouse click selects a name,
and Enter inserts it into the original template and loads the query prompt.
The first row keeps the current typed filter, so `timeout` can still query multiple
variables. Typing after a selection returns to this free-input option.

Names are requested only when opening that form, in the background, and cached
for the current connection/thread. A separate same-auth connection performs only
`SHOW GLOBAL VARIABLES`; it does not consume a pending result, commit a transaction
or replay the main connection's init command. Only names are retained. Timeouts or
metadata permissions leave manual input available. The ordinary favorite list,
editor and other parameter forms do not request variable metadata.

Persistence uses the existing favorite-query configuration. Saves write a temporary
file before replacing the configuration, preserving its permissions and symlink
target. A failed staged write leaves the previous file intact. Editing a shared
favorite creates a local override in the user configuration; it does not modify
the shared file. A successful save refreshes local favorite aliases without a
database query. Browsing and editing favorites themselves need no database request;
variable-name suggestions are the lazy metadata exception described above.

The browser does not synchronize user SQL into chezmoi or Git. If you manage the
configuration through chezmoi, keep using your existing configuration-sync flow.
The existing `\fs`, `\fd`, `\favorite edit` (external editor) and
`\favorite eval` commands remain available.
