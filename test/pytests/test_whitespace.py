from __future__ import annotations

import pytest

from mycli.packages.whitespace import strip_invisible_outside_literals

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
def test_invisible_characters_are_replaced(text: str, expected: str) -> None:
    assert strip_invisible_outside_literals(text) == expected


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


@pytest.mark.parametrize(
    'comment',
    ["-- don't", "# don't", "/* don't */", '-- say "hi', '/* `x */', "--\tdon't"],
)
def test_a_quote_inside_a_comment_does_not_protect_the_code_after_it(comment: str) -> None:
    sql = f'SELECT 1 {comment}\nFROM{NBSP}t'

    assert strip_invisible_outside_literals(sql) == f'SELECT 1 {comment}\nFROM t'


def test_comments_are_cleaned_and_literals_after_them_still_protected() -> None:
    sql = f"SELECT 1 -- it's{NBSP}here\n, 'a{NBSP}b'{NBSP}/* don't{NBSP}*/ FROM t"

    assert strip_invisible_outside_literals(sql) == f"SELECT 1 -- it's here\n, 'a{NBSP}b' /* don't */ FROM t"


def test_a_line_separator_ends_a_line_comment() -> None:
    """It becomes a newline, so what follows is code again, literals included."""
    sql = f"-- don't\u2028SELECT 'a{NBSP}b'"

    assert strip_invisible_outside_literals(sql) == f"-- don't\nSELECT 'a{NBSP}b'"


@pytest.mark.parametrize(
    'sql',
    [
        f"SELECT 1--'a{NBSP}b'",  # `--` without a space is arithmetic, not a comment
        f"SELECT /*! 'a{NBSP}b' */ 1",  # executed by MySQL
        f"SELECT /*+ SET_VAR(x='a{NBSP}b') */ 1",  # optimizer hint
    ],
)
def test_code_that_only_looks_like_a_comment_keeps_its_literals(sql: str) -> None:
    assert NBSP in strip_invisible_outside_literals(sql)


@pytest.mark.parametrize(
    ('before', 'pasted', 'expected'),
    [
        # Inside an open literal the paste is data, up to the quote that closes it.
        ("WHERE name = '", "Kim Min Su'", "Kim Min Su'"),
        ("WHERE name = '", "Kim Su' AND x = 1", "Kim Su' AND x = 1"),
        ('WHERE `col', ' x`', ' x`'),
        ("WHERE a = 'it''", "s x'", "s x'"),
        ("WHERE a = 'x\\", "' '", "' '"),  # the backslash escapes the pasted quote
        # Inside an open comment the paste is cleaned like the comment, up to its end.
        ('SELECT 1 /* note', ' x */ ', ' x */ '),
        ('SELECT 1 -- note', " it's\nSELECT ' '", " it's\nSELECT ' '"),
        # After a closed literal the paste is code again.
        ("WHERE name = 'a'", ' AND b', ' AND b'),
    ],
)
def test_the_text_before_sets_the_starting_state(before, pasted, expected):
    assert strip_invisible_outside_literals(pasted, before) == expected
