*************
Key Bindings:
*************

Most key bindings are simply inherited from `prompt-toolkit <https://python-prompt-toolkit.readthedocs.io/en/master/index.html>`_ .

The following key bindings are special to mycli:

Interactive commands (no function keys)
======================================

``\keys`` lists available interactive commands. ``\help`` shows CLI help.
``\smart`` toggles smart completion, ``\multiline`` toggles multiline input,
and ``\mode`` switches Vi/Emacs mode. ``\explain SQL`` previews a statement's
EXPLAIN plan. ``\browse`` searches tables and shows columns, indexes and DDL.

The fork no longer binds function keys or their alternate escape sequences.
Existing Ctrl+T, history, editing and formatting bindings remain available.

##
Up
##

With ``atuin_history`` and ``atuin_keys`` both on, open atuin's search, seeded with the buffer, as ``atuin init zsh``
binds it. Tab puts the pick in the buffer for editing; Enter does too, or runs it when atuin's ``enter_accept`` is
set. The search only covers what mycli stored in atuin; Ctrl-R stays on fzf. Up still moves through a multi-line
buffer and the completion menu, and falls back to the previous history entry if atuin cannot be run.

###
Tab
###

Force autocompletion at cursor.

#######
C-space
#######

Initialize autocompletion at cursor.

If the autocompletion menu is not showing, display it with the appropriate completions for the context.

If the menu is showing, select the next completion.

#########
ESC Enter
#########

Introduce a line break in multi-line mode, or dispatch the command in single-line mode.

The sequence ESC-Enter is often sent by Alt-Enter.

##################
C-x p (Emacs-mode)
##################

Prettify and indent current statement, usually into multiple lines.

Only accepts buffers containing single SQL statements.

##################
C-x u (Emacs-mode)
##################

Unprettify and dedent current statement, usually into one line.

Only accepts buffers containing single SQL statements.

##################
C-o d (Emacs-mode)
##################

Insert the current date at cursor, defined by NOW() on the server.

####################
C-o C-d (Emacs-mode)
####################

Insert the quoted current date at cursor.

##################
C-o t (Emacs-mode)
##################

Insert the current datetime at cursor.

####################
C-o C-t (Emacs-mode)
####################

Insert the quoted current datetime at cursor.
