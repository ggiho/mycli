from __future__ import annotations

import pytest

from mycli.favorite_parameters import parameter_names, render_parameters


@pytest.mark.parametrize(
    'query,names,dynamic',
    [
        ("SHOW GLOBAL VARIABLES LIKE '%{{ kv.n }}%'", ['n'], False),
        ("SELECT $10, $1, $10, '{{ kv.name }}'", ['$1', '$10', 'name'], False),
        ("SELECT '{{ kv.get(kv.which) }}'", ['which'], True),
        ('SELECT {{ 1 + 2 }}', [], False),
        ('SELECT 23', [], False),
    ],
)
def test_discovers_named_and_numbered_parameters(query: str, names: list[str], dynamic: bool) -> None:
    assert parameter_names(query) == (names, dynamic)


@pytest.mark.parametrize(
    'query,values,extra,expected',
    [
        ("SHOW GLOBAL VARIABLES LIKE '%{{ kv.n }}%'", {'n': 'timeout'}, '', "SHOW GLOBAL VARIABLES LIKE '%timeout%'"),
        ("SHOW GLOBAL VARIABLES LIKE '%{{ kv.n }}%'", {'n': ''}, '', "SHOW GLOBAL VARIABLES LIKE '%%'"),
        ("SELECT '{{ kv.get('name', 'default') }}'", {'name': ''}, '', "SELECT 'default'"),
        ("SELECT '{{ kv.name }}'", {'name': 'Kim Min Su'}, '', "SELECT 'Kim Min Su'"),
        ('SELECT $10, $1, $10', {'$1': 'one', '$10': 'ten'}, '', 'SELECT ten, one, ten'),
        ('SELECT $2', {'$2': 'two'}, '', 'SELECT two'),
        (
            "SELECT $1, $2, '{{ kv.name }}'",
            {'$1': '{{ 7*7 }} $2', '$2': 'second', 'name': '$1 {{ 3*3 }}'},
            '',
            "SELECT {{ 7*7 }} $2, second, '$1 {{ 3*3 }}'",
        ),
        ("SELECT '{{ kv[kv.which] }}'", {'which': 'name'}, '--name="Kim Min Su"', "SELECT 'Kim Min Su'"),
        ('SELECT {{ 1 + 2 }}', {}, '', 'SELECT 3'),
        ('SELECT 23', {}, '', 'SELECT 23'),
    ],
)
def test_renders_without_reinterpreting_parameter_values(query: str, values: dict[str, str], extra: str, expected: str) -> None:
    assert render_parameters(query, values, extra) == expected


@pytest.mark.parametrize(
    'query,values,extra',
    [
        ('SELECT $1', {}, ''),
        ('SELECT 23', {'name': 'unused'}, ''),
        ("SELECT '{{ kv.name }}'", {'name': 'first'}, '--name=second'),
        ("SELECT '{{ kv.name }}'", {}, 'unnamed'),
        ("SELECT '{{ kv.name }}'", {}, '--name="unterminated'),
        ("SELECT '{{ kv.name }}'", {}, '--unexpected=value'),
    ],
)
def test_invalid_parameter_input_is_reported(query: str, values: dict[str, str], extra: str) -> None:
    with pytest.raises(ValueError):
        render_parameters(query, values, extra)
