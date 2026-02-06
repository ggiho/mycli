"""Shared constants for mycli."""
from __future__ import annotations

from collections import namedtuple

# Query tuples are used for maintaining history
Query = namedtuple("Query", ["query", "successful", "mutating"])

SUPPORT_INFO = "Home: http://mycli.net\nBug tracker: https://github.com/dbcli/mycli/issues"
DEFAULT_WIDTH = 80
DEFAULT_HEIGHT = 25
