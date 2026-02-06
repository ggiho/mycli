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
