"""atuin's interactive history picker, wired to look like the user's shell.

The invocation mirrors what ``atuin init zsh`` generates, so the UI, keys and
filter mode match the shell:

* atuin draws its TUI on **stdout** and prints the chosen command on
  **stderr** (which is why the zsh widget swaps the two with ``3>&1 1>&2 2>&3``).
* ``ATUIN_QUERY`` seeds the search box with the current buffer.
* With ``enter_accept`` set, Enter returns the pick behind an
  ``__atuin_accept__:`` marker, meaning "run this now"; Tab returns it bare,
  meaning "put it in the buffer for editing".
* The picker ignores ``--author`` and ``--cwd`` but does honour
  ``--filter-mode session``, so :func:`session_id` gives every mycli entry one
  deterministic session to scope on. Without it the picker would offer shell
  commands, which ``enter_accept`` would then run as SQL.
"""

import hashlib
import logging
import os
from shutil import which
import subprocess

from prompt_toolkit.key_binding.key_processor import KeyPressEvent

logger = logging.getLogger(__name__)

ACCEPT_PREFIX = "__atuin_accept__:"


def is_available() -> bool:
    return which("atuin") is not None


def session_id(author: str) -> str:
    """Stable synthetic atuin session id for mycli's entries.

    Namespaced so it can never collide with another tool sharing the database.
    """
    return hashlib.sha256(f"mycli-history:{author}".encode()).hexdigest()[:32]


def search_history(event: KeyPressEvent, author: str, up_key_binding: bool = False) -> bool:
    """Let atuin pick a history entry and put it in the buffer.

    Returns False if atuin could not be run, so the caller can fall back.
    """
    buffer = event.current_buffer

    args = ["atuin", "search", "-i", "--filter-mode", "session"]
    if up_key_binding:
        # Tells atuin it was opened from the up-arrow, which it treats
        # differently from ctrl-r (see atuin's shell integration).
        args.append("--shell-up-key-binding")

    env = dict(
        os.environ,
        ATUIN_SHELL="zsh",
        ATUIN_QUERY=buffer.text,
        ATUIN_SESSION=session_id(author),
    )

    try:
        # No timeout: the user drives this UI. stdout stays on the terminal so
        # the TUI draws; the pick comes back on stderr.
        proc = subprocess.run(args, stdout=None, stderr=subprocess.PIPE, text=True, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("atuin search could not run: %s", e)
        return False

    # Redraw: atuin left the alternate screen and prompt_toolkit does not know.
    event.app.renderer.reset()
    event.app.invalidate()

    pick = proc.stderr.strip()
    if not pick:
        # Escape / no match. The key was still handled.
        return True

    accept = pick.startswith(ACCEPT_PREFIX)
    if accept:
        pick = pick[len(ACCEPT_PREFIX) :]

    buffer.text = pick
    buffer.cursor_position = len(pick)
    if accept:
        buffer.validate_and_handle()
    return True
