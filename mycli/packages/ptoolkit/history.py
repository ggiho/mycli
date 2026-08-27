from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import lru_cache
from itertools import islice
import logging
import os
import re
import subprocess
import threading

from prompt_toolkit.history import FileHistory
from sqlglot import Token, TokenType, tokenize
from sqlglot.errors import TokenError

from mycli.packages.ptoolkit import atuin
from mycli.packages.sql_utils import is_password_change

logger = logging.getLogger(__name__)

_StrOrBytesPath = str | bytes | os.PathLike[str] | os.PathLike[bytes]
FRECENCY_HISTORY_ENTRIES = 1000
FRECENCY_REFRESH_INTERVAL = 50
_FRECENCY_LITERAL_TYPES = frozenset({
    TokenType.STRING,
    TokenType.NUMBER,
    TokenType.BIT_STRING,
    TokenType.HEX_STRING,
    TokenType.BYTE_STRING,
    TokenType.NATIONAL_STRING,
    TokenType.RAW_STRING,
    TokenType.HEREDOC_STRING,
    TokenType.UNICODE_STRING,
})
_FRECENCY_WORD_PATTERN = re.compile(r'^[^\W\d][\w$]*(?:\s+[^\W\d][\w$]*)*$')


def _normalize_frecency_token(token: Token) -> str | None:
    if token.token_type in _FRECENCY_LITERAL_TYPES:
        return None
    if token.token_type != TokenType.IDENTIFIER and not _FRECENCY_WORD_PATTERN.fullmatch(token.text):
        return None
    return token.text.casefold() or None


def _calculate_frecency(
    entries: Iterable[str],
    history_entries: int = FRECENCY_HISTORY_ENTRIES,
) -> dict[str, float]:
    frecency: defaultdict[str, float] = defaultdict(float)
    for position, entry in enumerate(islice(entries, max(0, history_entries))):
        weight = 1 / (position + 1)
        try:
            tokens = tokenize(entry, dialect='mysql')
        except TokenError:
            continue
        for token in tokens:
            if normalized := _normalize_frecency_token(token):
                frecency[normalized] += weight
    return dict(frecency)


@lru_cache(maxsize=8192)
def _frecency_tokens(text: str) -> tuple[str, ...]:
    """Tokenize a completion candidate. Cached: candidates repeat on every keystroke."""
    try:
        tokens = tokenize(text, dialect='mysql')
    except TokenError:
        return ()
    return tuple(normalized for token in tokens if (normalized := _normalize_frecency_token(token)))


def frecency_score(text: str, frecency: Mapping[str, float]) -> float:
    normalized_tokens = _frecency_tokens(text)
    if not normalized_tokens:
        return 0.0
    return sum(frecency.get(token, 0.0) for token in normalized_tokens) / len(normalized_tokens)


class FileHistoryWithTimestamp(FileHistory):
    """
    :class:`.FileHistory` class that stores all strings in a file with timestamp.
    """

    def __init__(
        self,
        filename: _StrOrBytesPath,
        frecency_history_entries: int = FRECENCY_HISTORY_ENTRIES,
        frecency_refresh_interval: int = FRECENCY_REFRESH_INTERVAL,
    ) -> None:
        self.filename = filename
        super().__init__(filename)
        self.frecency_history_entries = max(0, frecency_history_entries)
        self.frecency_refresh_interval = max(0, frecency_refresh_interval)
        self._frecency: dict[str, float] = {}
        self._frecency_lock = threading.Lock()
        self._frecency_generation = 0
        self._frecency_thread: threading.Thread | None = None
        self._frecency_entries_since_refresh = 0
        if self.frecency_history_entries:
            self._request_frecency_refresh()

    @property
    def frecency(self) -> dict[str, float]:
        with self._frecency_lock:
            return self._frecency

    def _request_frecency_refresh(self) -> None:
        with self._frecency_lock:
            self._frecency_generation += 1
            if self._frecency_thread is not None:
                return
            thread = threading.Thread(target=self._refresh_frecency, name='frecency_refresh', daemon=True)
            self._frecency_thread = thread

        try:
            thread.start()
        except Exception:
            with self._frecency_lock:
                if self._frecency_thread is thread:
                    self._frecency_thread = None
            logger.exception('Failed to start history frecency calculation.')

    def refresh_frecency(self) -> None:
        """Request an immediate background refresh of history frecency."""
        if not self.frecency_history_entries:
            return
        with self._frecency_lock:
            self._frecency_entries_since_refresh = 0
        self._request_frecency_refresh()

    def _refresh_frecency(self) -> None:
        while True:
            with self._frecency_lock:
                generation = self._frecency_generation

            try:
                frecency = _calculate_frecency(self.load_history_strings(), self.frecency_history_entries)
            except Exception:
                logger.exception('Failed to calculate history frecency.')
                with self._frecency_lock:
                    if self._frecency_generation != generation:
                        continue
                    self._frecency_thread = None
                return

            with self._frecency_lock:
                self._frecency = frecency
                if self._frecency_generation == generation:
                    self._frecency_thread = None
                    return

    def append_string(self, string: str) -> None:
        "Add string to the history."
        self._loaded_strings.insert(0, string)
        if is_password_change(string):
            return
        self.store_string(string)
        if not self.frecency_history_entries or not self.frecency_refresh_interval:
            return
        with self._frecency_lock:
            self._frecency_entries_since_refresh += 1
            refresh = self._frecency_entries_since_refresh >= self.frecency_refresh_interval
            if refresh:
                self._frecency_entries_since_refresh = 0
        if refresh:
            self._request_frecency_refresh()

    def load_history_with_timestamp(self) -> list[tuple[str, str]]:
        """
        Load history entries along with their timestamps.

        Returns:
            list[tuple[str, str]]: A list of tuples where each tuple contains
                                   a history entry and its corresponding timestamp.
        """
        history_with_timestamp: list[tuple[str, str]] = []
        lines: list[str] = []
        timestamp: str = ""

        def add() -> None:
            if lines:
                # Join and drop trailing newline.
                string = "".join(lines)[:-1]
                history_with_timestamp.append((string, timestamp))

        if os.path.exists(self.filename):
            with open(self.filename, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith("#"):
                        # Extract timestamp
                        timestamp = line[2:].strip()
                    elif line.startswith("+"):
                        lines.append(line[1:])
                    else:
                        add()
                        lines = []

                add()

        return list(reversed(history_with_timestamp))


class AtuinHistory(FileHistoryWithTimestamp):
    """History stored in atuin instead of the history file.

    Subclasses :class:`FileHistoryWithTimestamp` rather than
    :class:`~prompt_toolkit.history.History` so that everything keyed off that
    type keeps working: frecency refresh, the ``is_password_change`` filter in
    :meth:`FileHistoryWithTimestamp.append_string`, the fzf reverse search, and
    the ``\\#`` rehash hook.

    Entries are tagged with an author (``mycli`` by default) so SQL stays
    distinguishable from shell history::

        atuin search --author mycli

    Only what this class wrote lives in atuin, so the inherited file reader is
    used as the tail: entries from ``filename`` are appended after the atuin
    ones. Queries predating the switch stay reachable without being imported,
    and nothing new is written to the file.

    Behaviour of the atuin CLI this relies on (verified against atuin 18.19):

    * ``atuin search`` prints oldest-first; ``--reverse`` is what yields the
      newest-first order :meth:`load_history_strings` must return. Its
      ``--help`` describes the opposite.
    * ``--include-duplicates`` is required, because frecency scores how often
      an entry appears.
    * ``history end`` is not called: it demands an exit code we do not have,
      since the line is stored on accept, before the query runs. atuin leaves
      such entries at ``exit=-1``, which is the honest value.
    """

    #: atuin is a local sqlite read; this only guards against a wedged process.
    TIMEOUT = 5.0
    #: {command} goes last so a tab inside the SQL cannot shift the split.
    _FORMAT = "{time}\t{command}"

    def __init__(self, filename: _StrOrBytesPath, author: str = "mycli", limit: int = 5000, **kwargs) -> None:
        self.author = author
        self.limit = limit
        super().__init__(filename, **kwargs)

    def _atuin(self, *args: str, env: Mapping[str, str] | None = None) -> str | None:
        """Run atuin and return stdout, or None if it could not be run."""
        try:
            proc = subprocess.run(("atuin", *args), capture_output=True, text=True, timeout=self.TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug('atuin %s failed: %s', args[:1], e)
            return None
        if proc.returncode != 0:
            logger.debug('atuin %s exited %s: %s', args[:1], proc.returncode, proc.stderr.strip())
            return None
        return proc.stdout

    def _search(self, fmt: str | None) -> list[str] | None:
        args = [
            'search',
            '--author',
            self.author,
            '--include-duplicates',
            '--reverse',
            '--print0',
            '--limit',
            str(self.limit),
        ]
        args += ['--format', fmt] if fmt else ['--cmd-only']
        out = self._atuin(*args)
        if out is None:
            return None
        # --print0 terminates every record, so the trailing split is empty.
        return [record for record in out.split('\0') if record]

    def load_history_strings(self) -> Iterable[str]:
        entries = self._search(None)
        if entries is None:
            logger.debug('atuin unreadable; using the history file only')
            entries = []
        # The inherited reader supplies the pre-atuin tail.
        entries.extend(super().load_history_strings())
        return entries

    def store_string(self, string: str) -> None:
        # append_string() has already dropped password changes and updated the
        # frecency counters; only the storage backend differs here.
        self._atuin(
            'history',
            'start',
            '--author',
            self.author,
            string,
            env=dict(os.environ, ATUIN_SESSION=atuin.session_id(self.author)),
        )

    def load_history_with_timestamp(self) -> list[tuple[str, str]]:
        records = self._search(self._FORMAT)
        if records is None:
            return super().load_history_with_timestamp()
        entries = []
        for record in records:
            timestamp, separator, command = record.partition('\t')
            if not separator:
                # Malformed record: keep it as a command rather than silently
                # presenting the query text as a timestamp.
                timestamp, command = '', timestamp
            entries.append((command, timestamp))
        entries.extend(super().load_history_with_timestamp())
        return entries
