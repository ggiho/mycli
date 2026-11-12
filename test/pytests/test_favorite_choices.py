from __future__ import annotations

import gc
from typing import Any
from unittest.mock import MagicMock
import weakref

import pytest

from mycli import favorite_choices
from mycli.favorite_choices import VariableNameCache, is_global_variables_query


@pytest.mark.parametrize(
    'query,expected',
    [
        ("SHOW GLOBAL VARIABLES LIKE '%{{ kv.n }}%'", True),
        ("/* hint */ show global variables like '%{{ kv.pattern }}%'", True),
        ("SHOW GLOBAL VARIABLES LIKE '%$1%'", True),
        ("SHOW SESSION VARIABLES LIKE '%{{ kv.n }}%'", False),
        ("SELECT '{{ kv.n }}'", False),
        ("SHOW GLOBAL STATUS LIKE '%{{ kv.n }}%'", False),
        ("SHOW GLOBAL VARIABLES LIKE 'x'; SELECT 1", False),
    ],
)
def test_variable_names_only_for_matching_metadata_query(query: str, expected: bool) -> None:
    assert is_global_variables_query(query) is expected


class Source:
    open = True
    host = '127.0.0.1'
    port = 3306
    user = 'example'
    password = b'example-only'
    unix_socket = '/tmp/example.sock'
    charset = 'utf8mb4'
    ssl = True
    ctx = object()
    server_public_key = None
    init_command = 'DO NOT REPLAY'

    def __init__(self) -> None:
        self.cursor = MagicMock()
        self.thread = 7

    def thread_id(self) -> int:
        return self.thread


@pytest.fixture
def metadata(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        ('max_connections', '100'),
        ('connect_timeout', '10'),
        ('max_connections', '200'),
    ]
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(favorite_choices.pymysql, 'connect', connect)
    return connect, connection


def test_lazy_cache_reads_names_on_an_independent_connection(metadata: tuple[MagicMock, MagicMock]) -> None:
    connect, connection = metadata
    source: Any = Source()
    cache = VariableNameCache()
    connect.assert_not_called()
    names = cache.get(source)
    assert names == ['connect_timeout', 'max_connections']
    names.append('caller mutation')
    assert cache.get(source) == ['connect_timeout', 'max_connections']
    connect.assert_called_once()
    kwargs = connect.call_args.kwargs
    assert kwargs['host'] == source.host and kwargs['port'] == source.port
    assert kwargs['unix_socket'] == source.unix_socket and kwargs['ssl'] is source.ctx
    assert kwargs['connect_timeout'] == kwargs['read_timeout'] == kwargs['write_timeout'] == 5
    assert 'init_command' not in kwargs
    connection.cursor.return_value.__enter__.return_value.execute.assert_called_once_with('SHOW GLOBAL VARIABLES')
    connection.close.assert_called_once_with()
    source.cursor.assert_not_called()


def test_cache_changes_with_connection_and_reconnection(metadata: tuple[MagicMock, MagicMock]) -> None:
    connect, _connection = metadata
    source: Any = Source()
    cache = VariableNameCache()
    cache.get(source)
    source.thread = 8
    cache.get(source)
    cache.get(Source())  # type: ignore[arg-type]
    assert connect.call_count == 3


def test_metadata_failure_closes_connection_and_can_retry(metadata: tuple[MagicMock, MagicMock]) -> None:
    connect, connection = metadata
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = OSError('metadata unavailable')
    source: Any = Source()
    cache = VariableNameCache()
    with pytest.raises(OSError):
        cache.get(source)
    connection.close.assert_called_once_with()
    source.cursor.assert_not_called()
    cursor.execute.side_effect = None
    assert cache.get(source) == ['connect_timeout', 'max_connections']
    assert connect.call_count == 2


def test_cache_does_not_retain_closed_result_connections(metadata: tuple[MagicMock, MagicMock]) -> None:
    source: Any = Source()
    cache = VariableNameCache()
    cache.get(source)
    reference = weakref.ref(source)
    del source
    gc.collect()
    assert reference() is None
