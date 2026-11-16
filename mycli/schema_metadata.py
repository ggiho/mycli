"""Read-only metadata for the interactive schema browser."""

from __future__ import annotations

import pymysql
from pymysql.connections import Connection

from mycli.schema_model import Column, Index, Structure


def quote_identifier(value: str) -> str:
    return '`' + value.replace('`', '``') + '`'


class SchemaMetadata:
    def __init__(self, source: Connection) -> None:
        self.source = source

    def _connect(self) -> Connection:
        source = self.source
        if not source.open:
            raise RuntimeError('Database connection is closed.')
        return pymysql.connect(
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
            program_name='mycli-schema-browser',
        )

    def tables(self) -> list[tuple[str, str, str]]:
        connection = self._connect()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys') "
                    "ORDER BY TABLE_SCHEMA,TABLE_NAME LIMIT 2000"
                )
                return [(str(schema), str(name), str(kind)) for schema, name, kind in cur.fetchall()]
        finally:
            connection.close()

    def describe(self, schema: str, name: str) -> Structure:
        connection = self._connect()
        qualified = f'{quote_identifier(schema)}.{quote_identifier(name)}'
        result = Structure()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    'SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY,COLUMN_DEFAULT,COLUMN_COMMENT '
                    'FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION',
                    (schema, name),
                )
                result.columns = [
                    Column(
                        str(column),
                        str(datatype),
                        nullable == 'YES',
                        key == 'PRI',
                        None if default is None else str(default),
                        str(comment or ''),
                    )
                    for column, datatype, nullable, key, default, comment in cur.fetchall()
                ]
                cur.execute(
                    'SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.STATISTICS '
                    'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX',
                    (schema, name),
                )
                grouped: dict[str, tuple[bool, list[str]]] = {}
                for index_name, non_unique, _position, column in cur.fetchall():
                    unique, columns = grouped.setdefault(str(index_name), (not bool(non_unique), []))
                    columns.append(str(column) if column is not None else '(expression)')
                result.indexes = [
                    Index(index_name, f'{"UNIQUE " if unique else ""}({", ".join(columns)})')
                    for index_name, (unique, columns) in grouped.items()
                ]
                try:
                    cur.execute('SHOW CREATE TABLE ' + qualified)
                    row = cur.fetchone()
                    if row:
                        result.definition = str(row[1])
                except pymysql.MySQLError:
                    result.note = 'SQL definition is unavailable with the current permissions.'
                return result
        finally:
            connection.close()

    @staticmethod
    def select_sql(schema: str, name: str) -> str:
        return f'SELECT * FROM {quote_identifier(schema)}.{quote_identifier(name)} LIMIT 100;'

    @staticmethod
    def qualified_name(schema: str, name: str) -> str:
        return f'{quote_identifier(schema)}.{quote_identifier(name)}'
