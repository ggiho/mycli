"""Bounded cursor ownership and conservative query eligibility for the SQL viewer."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import threading
from typing import Any

import sqlparse
from sqlparse import sql, tokens


def simple_relation(statement: str) -> tuple[str | None, str] | None:
    """Accept one ordinary table SELECT, without changing the user's SQL."""
    if '/*!' in statement or len(sqlparse.split(statement)) != 1:
        return None
    parsed = sqlparse.parse(statement)
    if not parsed or parsed[0].get_type() != 'SELECT':
        return None

    def has_function(group: Any) -> bool:
        return isinstance(group, sql.Function) or any(has_function(t) for t in getattr(group, 'tokens', []))

    if has_function(parsed[0]):
        return None
    forbidden = {
        'WITH',
        'INTO',
        'FOR',
        'FOR UPDATE',
        'FOR SHARE',
        'LOCK',
        'UNION',
        'INTERSECT',
        'EXCEPT',
        'GROUP BY',
        'HAVING',
        'DISTINCT',
        'PROCEDURE',
        'SQL_CALC_FOUND_ROWS',
    }
    flat = [t for t in parsed[0].flatten() if not t.is_whitespace and t.ttype not in tokens.Comment]
    if sum(t.ttype in tokens.Keyword.DML and t.normalized == 'SELECT' for t in flat) != 1:
        return None
    for token in flat:
        if token.ttype in tokens.Keyword and (
            ' '.join(token.normalized.split()) in forbidden or 'JOIN' in token.normalized or token.normalized.startswith(('LOCK ', 'FOR '))
        ):
            return None
        if token.ttype not in tokens.Literal.String and (token.value.startswith('@') or token.value == '::'):
            return None
    top = [t for t in parsed[0].tokens if not t.is_whitespace and t.ttype not in tokens.Comment]
    for index, token in enumerate(top):
        if token.ttype in tokens.Keyword and token.normalized == 'FROM' and index + 1 < len(top):
            relation = top[index + 1]
            if not isinstance(relation, sql.Identifier) or any(isinstance(t, sql.Parenthesis) for t in relation.tokens):
                return None
            name = relation.get_real_name()
            return (relation.get_parent_name(), name) if name else None
    return None


class PagedRows:
    """A live iterator with explicit cancellation; never eagerly exhaust it."""

    def __init__(
        self, iterator: Iterator[tuple], description: Any, close: Callable[[], None], cancel: Callable[[], None], *, live: bool
    ) -> None:
        self.iterator = iterator
        self.description = description or ()
        self.headers = [str(c[0]) for c in self.description]
        self.preview: list[tuple] = []
        self.live = live
        self.exhausted = False
        self.closed = False
        self.cancelled = False
        self._cancel_lock = threading.Lock()
        self._close = close
        self._cancel = cancel

    def __iter__(self) -> Iterator[tuple]:
        # Accidental formatter use must not drain an unbounded cursor.
        return iter(self.preview)

    def next_row(self) -> tuple:
        try:
            return next(self.iterator)
        except StopIteration:
            self.exhausted = True
            raise

    def cancel(self) -> None:
        with self._cancel_lock:
            if not self.live or self.exhausted or self.cancelled:
                return
            self.cancelled = True
        try:
            self._cancel()
        except Exception:
            with self._cancel_lock:
                self.cancelled = False
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.cancel()
        self._close()
        self.closed = True


def mysql_stream(executor: Any, statement: str) -> PagedRows | None:
    import pymysql
    from pymysql.constants import SERVER_STATUS

    connection = executor.conn
    relation = simple_relation(statement)
    if not relation or not connection.get_autocommit() or connection.server_status & SERVER_STATUS.SERVER_STATUS_IN_TRANS:
        return None
    schema, name = relation
    quoted = '.'.join('`' + p.replace('`', '``') + '`' for p in [schema, name] if p)
    try:
        with connection.cursor() as meta:
            meta.execute('SHOW CREATE TABLE ' + quoted)
            row = meta.fetchone()
            if not row or 'VIEW' in str(meta.description[1][0]).upper() or 'CREATE TEMPORARY TABLE' in str(row[1]).upper():
                return None
    except pymysql.MySQLError:
        return None
    cursor = connection.cursor(pymysql.cursors.SSCursor)
    cursor.execute(statement)

    def cancel() -> None:
        if not connection.open or not getattr(cursor._result, 'unbuffered_active', False):
            return
        control = pymysql.connect(
            host=connection.host,
            port=connection.port,
            user=connection.user,
            password=connection.password,
            unix_socket=connection.unix_socket,
            charset=connection.charset,
            ssl=connection.ctx if connection.ssl else None,
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
        )
        try:
            with control.cursor() as command:
                command.execute(f'KILL QUERY {int(connection.thread_id())}')
        finally:
            control.close()

    def close() -> None:
        if not connection.open:
            cursor.connection = None
            return
        try:
            cursor.close()
        except pymysql.OperationalError as error:
            if error.args[0] != 1317:
                raise
            cursor.close()  # PyMySQL marked the interrupted result inactive.

    return PagedRows(iter(cursor), cursor.description, close, cancel, live=True)
