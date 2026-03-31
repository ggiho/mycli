"""Query utility functions for mycli."""
from __future__ import annotations

import logging
import sqlparse

logger = logging.getLogger(__name__)


def need_completion_refresh(queries: str) -> bool:
    """Determines if the completion needs a refresh by checking if the sql
    statement is an alter, create, drop or change db."""
    for query in sqlparse.split(queries):
        try:
            first_token = query.split()[0]
            if first_token.lower() in ("alter", "create", "use", "\\r", "\\u", "connect", "drop", "rename"):
                return True
        except Exception as e:
            logger.debug("Exception in query parsing: %r", e)
            return False
    return False


def need_completion_reset(queries: str) -> bool:
    """Determines if the statement is a database switch such as 'use' or '\\u'.
    When a database is changed the existing completions must be reset before we
    start the completion refresh for the new database.
    """
    for query in sqlparse.split(queries):
        try:
            first_token = query.split()[0]
            if first_token.lower() in ("use", "\\u"):
                return True
        except Exception as e:
            logger.debug("Exception in query parsing: %r", e)
            return False
    return False


_DDL_REFRESHER_MAP: dict[str, set[str]] = {
    "create": {"schemata", "tables", "views", "functions", "procedures", "enum_values"},
    "alter": {"tables", "views", "enum_values"},
    "drop": {"schemata", "tables", "views", "functions", "procedures"},
    "rename": {"tables", "views"},
}


def completion_refresh_scope(queries: str) -> set[str] | None:
    """Return the set of refresher names needed for the DDL, or None for full refresh.

    For USE/database switch returns None (full refresh needed).
    For CREATE/ALTER/DROP/RENAME returns only the relevant subset.
    """
    for query in sqlparse.split(queries):
        try:
            tokens = query.split()
            first = tokens[0].lower()
        except (IndexError, Exception):
            continue
        if first in ("use", "\\u", "\\r", "connect"):
            return None
        scope = _DDL_REFRESHER_MAP.get(first)
        if scope:
            second = tokens[1].lower() if len(tokens) > 1 else ""
            if second == "table":
                return {"schemata", "tables", "enum_values"}
            elif second == "view":
                return {"schemata", "views"}
            elif second in ("function", "procedure"):
                return {"functions", "procedures"}
            elif second == "database":
                return {"databases", "schemata"}
            return scope
    return None


def is_mutating(status: str | None) -> bool:
    """Determines if the statement is mutating based on the status."""
    if not status:
        return False

    mutating = {"insert", "update", "delete", "alter", "create", "drop", "replace", "truncate", "load", "rename"}
    return status.split(None, 1)[0].lower() in mutating


def is_select(status: str | None) -> bool:
    """Returns true if the first word in status is 'select'."""
    if not status:
        return False
    return status.split(None, 1)[0].lower() == "select"
