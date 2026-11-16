from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mycli import schema_metadata


def test_metadata_closes_connection_and_quotes_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    source = MagicMock()
    metadata = MagicMock()
    connect = MagicMock(return_value=metadata)
    monkeypatch.setattr(schema_metadata.pymysql, 'connect', connect)
    cursor = metadata.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [('id', 'int', 'NO', 'PRI', None, 'identifier')]
    cursor.fetchone.return_value = ('odd', 'CREATE TABLE `odd` (`id` int PRIMARY KEY)')
    provider = schema_metadata.SchemaMetadata(source)
    text = provider.describe('a`b', 'c`d')
    assert 'PRIMARY KEY' in text and 'NOT NULL' in text
    assert cursor.execute.call_args.args == ('SHOW CREATE TABLE `a``b`.`c``d`',)
    assert cursor.execute.call_args_list[0].args[1] == ('a`b', 'c`d')
    metadata.close.assert_called_once()
    source.cursor.assert_not_called()
    assert 'init_command' not in connect.call_args.kwargs
    assert provider.select_sql('a`b', 'c`d') == 'SELECT * FROM `a``b`.`c``d` LIMIT 100;'


def test_failed_metadata_also_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = MagicMock()
    metadata.cursor.return_value.__enter__.return_value.execute.side_effect = OSError('denied')
    monkeypatch.setattr(schema_metadata.pymysql, 'connect', lambda **kwargs: metadata)
    with pytest.raises(OSError):
        schema_metadata.SchemaMetadata(MagicMock()).tables()
    metadata.close.assert_called_once()
