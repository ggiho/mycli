from __future__ import annotations

import pytest

from mycli.packages.whitespace import strip_invisible, strip_invisible_outside_literals

NBSP = '\u00a0'


@pytest.mark.parametrize(
    'text, expected',
    [
        (f'id{NBSP}{NBSP}INT', 'id  INT'),
        ('a\u3000b', 'a b'),  # CJK full-width space
        ('a\u200bb', 'ab'),  # zero-width space is dropped, not spaced
        ('a\ufeffb', 'ab'),  # BOM mid-text
        ('a\u2028b', 'a\nb'),  # line separator
        ('plain text', 'plain text'),
    ],
)
def test_strip_invisible(text: str, expected: str) -> None:
    assert strip_invisible(text) == expected


def test_literals_are_left_untouched() -> None:
    """Inside quotes the characters are data; rewriting them corrupts a value."""
    sql = f"INSERT INTO t VALUES ('a{NBSP}b'){NBSP}{NBSP};"

    assert strip_invisible_outside_literals(sql) == f"INSERT INTO t VALUES ('a{NBSP}b')  ;"


@pytest.mark.parametrize(
    'sql',
    [
        f"SELECT 'it''s{NBSP}here';",  # doubled quote escapes, does not close
        f"SELECT 'a\\'b{NBSP}c';",  # backslash escapes, does not close
        f'SELECT `col{NBSP}name`;',  # backtick identifier
        f'SELECT "a{NBSP}b";',  # double-quoted string
    ],
)
def test_quoting_forms_protect_their_contents(sql: str) -> None:
    assert NBSP in strip_invisible_outside_literals(sql)


def test_unterminated_quote_protects_the_remainder() -> None:
    """Erring towards changing nothing beats guessing where a literal ends."""
    assert strip_invisible_outside_literals(f"SELECT 'a{NBSP}b") == f"SELECT 'a{NBSP}b"


def test_structure_outside_a_literal_is_still_normalized() -> None:
    sql = f"SELECT{NBSP}'kept{NBSP}as is'{NBSP}FROM{NBSP}t;"

    assert strip_invisible_outside_literals(sql) == f"SELECT 'kept{NBSP}as is' FROM t;"
