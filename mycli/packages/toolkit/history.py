import os
from collections import Counter
from typing import Union

from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory

_StrOrBytesPath = Union[str, bytes, os.PathLike]


class FileHistoryWithTimestamp(FileHistory):
    """
    :class:`.FileHistory` class that stores all strings in a file with timestamp.
    """

    def __init__(self, filename: _StrOrBytesPath) -> None:
        self.filename = filename
        super().__init__(filename)

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


class FrequencyWeightedAutoSuggest(AutoSuggest):
    """Auto-suggest based on history, preferring frequently used and recent entries.

    Scores each candidate as: frequency_count + recency_bonus
    where recency_bonus = (total_entries - position) / total_entries
    """

    def get_suggestion(self, buffer: Buffer, document: Document) -> Suggestion | None:
        history = buffer.history
        text = document.text.rsplit("\n", 1)[-1]

        if not text.strip():
            return None

        history_lines = list(history.get_strings())
        if not history_lines:
            return None

        # Flatten to individual lines and count frequency
        all_lines: list[str] = []
        for entry in history_lines:
            for line in entry.splitlines():
                all_lines.append(line)

        freq = Counter(all_lines)
        total = len(all_lines)
        if total == 0:
            return None

        best_score = -1.0
        best_suggestion: str | None = None

        for idx, line in enumerate(reversed(all_lines)):
            if not line.startswith(text):
                continue
            recency = (idx + 1) / total  # more recent = higher
            score = freq[line] + recency
            if score > best_score:
                best_score = score
                best_suggestion = line

        if best_suggestion is not None:
            return Suggestion(best_suggestion[len(text):])
        return None
