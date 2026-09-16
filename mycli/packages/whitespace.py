"""Removal of invisible characters that rich-text sources paste into SQL.

Slack, Notion and Google Docs render messages as HTML, where a run of spaces
collapses to one. To keep alignment they emit U+00A0 (no-break space) for every
space after the first, and CJK editors emit U+3000. MySQL rejects those with a
1064 syntax error that points at text which looks perfectly ordinary on screen.
"""

# U+00A0 no-break space is the Slack/HTML one; U+3000 is the CJK full-width
# space; the rest are typographic spaces that word processors emit.
_SPACE_LIKE = '\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000'
_LINE_LIKE = '\u2028\u2029'  # line and paragraph separators
_ZERO_WIDTH = '\u200b\u2060\ufeff'  # zero-width space, word joiner, BOM

TRANSLATIONS: dict[int, str | None] = {ord(c): ' ' for c in _SPACE_LIKE}
TRANSLATIONS.update({ord(c): '\n' for c in _LINE_LIKE})
TRANSLATIONS.update({ord(c): None for c in _ZERO_WIDTH})

QUOTES = '\'"`'


def strip_invisible(text: str) -> str:
    """Replace invisible characters everywhere, literals included."""
    return text.translate(TRANSLATIONS)


def strip_invisible_outside_literals(text: str) -> str:
    """Replace invisible characters, leaving quoted literals byte for byte.

    Inside `'...'`, `"..."` and backticks the characters are data, so rewriting
    them would silently corrupt a value or an identifier. An unterminated quote
    protects the remainder of the text, which errs towards changing nothing.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]

        if quote is None:
            if char in QUOTES:
                quote = char
                out.append(char)
            else:
                replacement = TRANSLATIONS.get(ord(char), char)
                if replacement is not None:
                    out.append(replacement)
            i += 1
            continue

        # A backslash escapes the next character in strings, but not in
        # backtick-quoted identifiers.
        if char == '\\' and quote != '`' and i + 1 < len(text):
            out.append(text[i : i + 2])
            i += 2
            continue

        # A doubled quote is an escaped quote, not the end of the literal.
        if char == quote and i + 1 < len(text) and text[i + 1] == quote:
            out.append(char * 2)
            i += 2
            continue

        if char == quote:
            quote = None
        out.append(char)
        i += 1

    return ''.join(out)
