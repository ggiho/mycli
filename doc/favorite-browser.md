# Favorite query browser

In the interactive prompt, use `\f` (or `/f`) to search saved queries by name or
SQL text. The selected query appears in full in the preview. Arrow keys change
the selection; Enter or **Load** puts the SQL into the normal query prompt.
Edit it there and press Enter when ready to execute it. Loading does not update
the stored favorite.

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

Tab/Shift+Tab move between controls and buttons; buttons also support the mouse.
The SQL editor follows the configured Emacs/Vi mode. In the Vi editor, Escape
keeps its normal Vim meaning; use **Cancel** to leave. In the browser, Escape
closes it. The browser adapts to narrow terminals and follows the current SQL
theme and terminal background. No terminal-global shortcut is installed.

Template SQL such as `$1` or `{{ kv.name }}` is loaded and edited as written.
Replace those placeholders in the query prompt before executing the loaded SQL.
For automatic substitution, use the existing `\f name value` or
`\favorite eval name value` commands instead. Parameter-entry forms are not part
of this iteration.

Persistence uses the existing favorite-query configuration. Saves write a temporary
file before replacing the configuration, preserving its permissions and symlink
target. A failed staged write leaves the previous file intact. Editing a shared
favorite creates a local override in the user configuration; it does not modify
the shared file. A successful save refreshes local favorite aliases without a
database query. No database connection is needed to browse or edit favorites.

The browser does not synchronize user SQL into chezmoi or Git. If you manage the
configuration through chezmoi, keep using your existing configuration-sync flow.
The existing `\fs`, `\fd`, `\favorite edit` (external editor) and
`\favorite eval` commands remain available.
