"""Lazy server metadata used by favorite parameter suggestions."""

from __future__ import annotations

import re
import threading
from weakref import ReferenceType, ref

import pymysql
from pymysql.connections import Connection
import sqlparse


def is_global_variables_query(query: str) -> bool:
    if len(sqlparse.split(query)) != 1:
        return False
    clean = sqlparse.format(query, strip_comments=True).strip()
    return re.match(r'^SHOW\s+GLOBAL\s+VARIABLES\s+LIKE\b', clean, re.IGNORECASE) is not None


class VariableNameCache:
    """Cache names per live connection without reading its pending result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._source: ReferenceType[Connection] | None = None
        self._thread_id: int | None = None
        self._names: tuple[str, ...] | None = None

    def get(self, source: Connection) -> list[str]:
        with self._lock:
            if not source.open:
                raise RuntimeError('No active connection for variable suggestions.')
            thread_id = source.thread_id()
            if self._source is not None and self._source() is source and thread_id == self._thread_id and self._names is not None:
                return list(self._names)
            # Do not replay init_command or consume a streaming cursor on source.
            metadata = pymysql.connect(
                host=source.host,
                port=source.port,
                user=source.user,
                password=source.password,
                unix_socket=source.unix_socket,
                charset=source.charset,
                ssl=source.ctx if source.ssl else None,
                server_public_key=source.server_public_key,
                autocommit=True,
                connect_timeout=5,
                read_timeout=5,
                write_timeout=5,
                program_name='mycli-favorite-variables',
            )
            try:
                with metadata.cursor() as cursor:
                    cursor.execute('SHOW GLOBAL VARIABLES')
                    names = tuple(sorted({row[0] for row in cursor.fetchall() if isinstance(row[0], str)}, key=str.casefold))
            finally:
                metadata.close()
            self._source, self._thread_id, self._names = ref(source), thread_id, names
            return list(names)
