"""Read-only metadata for the interactive schema browser."""

from __future__ import annotations

import pymysql
from pymysql.connections import Connection


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

    def describe(self, schema: str, name: str) -> str:
        connection = self._connect()
        qualified = f'{quote_identifier(schema)}.{quote_identifier(name)}'
        try:
            with connection.cursor() as cur:
                cur.execute(
                    'SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY,COLUMN_DEFAULT,COLUMN_COMMENT '
                    'FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION',
                    (schema, name),
                )
                lines = [qualified, '', 'Columns']
                for column, datatype, nullable, key, default, comment in cur.fetchall():
                    lines.append(f'{column}  {datatype}  {"NULL" if nullable == "YES" else "NOT NULL"}  {key or ""}')
                    if default is not None:
                        lines.append(f'  Default: {default}')
                    if comment:
                        lines.append(f'  {comment}')
                cur.execute('SHOW CREATE TABLE ' + qualified)
                row = cur.fetchone()
                if row:
                    lines.extend(['', 'DDL (includes indexes and constraints)', str(row[1])])
                return '\n'.join(lines)
        finally:
            connection.close()

    @staticmethod
    def select_sql(schema: str, name: str) -> str:
        return f'SELECT * FROM {quote_identifier(schema)}.{quote_identifier(name)} LIMIT 100;'

    @staticmethod
    def qualified_name(schema: str, name: str) -> str:
        return f'{quote_identifier(schema)}.{quote_identifier(name)}'
