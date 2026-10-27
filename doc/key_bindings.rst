*************
Key Bindings:
*************

Most key bindings are simply inherited from `prompt-toolkit <https://python-prompt-toolkit.readthedocs.io/en/master/index.html>`_ .

The following key bindings are special to mycli:

###
F1
###

Open documentation index in a browser tab.

###
F2
###

Enable/Disable SmartCompletion Mode.

###
F3
###

Enable/Disable Multiline Mode.

###
F4
###

Toggle between Vi and Emacs mode.

###
F5
###

Show the ``EXPLAIN`` plan of the statement under the cursor, without running it or leaving the buffer.

In the blanks between statements, the cursor belongs to the statement before it. Only statements ``EXPLAIN``
accepts are explained (``SELECT``, ``INSERT``, ``UPDATE``, ``DELETE``, ``REPLACE``, ``TABLE`` and ``WITH``), and a
statement holding a ``/*! ... */`` comment is refused, since MySQL runs what that holds.

MySQL can execute a derived table or a subquery while planning, so a preview can take as long as that part of
the query. Ctrl-C cancels it, keeping the buffer and the connection.

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
