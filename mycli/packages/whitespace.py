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


def _comment_length(text: str, i: int) -> int:
    """Length of the comment starting at `text[i]`, or 0 if none starts there.

    A line comment stops before its line break. `--` opens one only when followed
    by whitespace or a control character, as in MySQL (`1--1` is arithmetic).
    `/*!` and `/*+` are executed by MySQL, so they are code, not comments.
    """
    if text.startswith('/*', i) and not text.startswith(('/*!', '/*+'), i):
        end = text.find('*/', i + 2)
        return (len(text) if end == -1 else end + 2) - i
    if text.startswith('--', i) and i + 2 < len(text):
        follower = TRANSLATIONS.get(ord(text[i + 2]), text[i + 2])
        is_line_comment = follower is not None and (follower.isspace() or ord(follower) < 32)
    else:
        is_line_comment = text.startswith('#', i)
    if is_line_comment:
        # U+2028/U+2029 become newlines too, so they end the comment as well.
        ends = (text.find(c, i) for c in '\n' + _LINE_LIKE)
        return min((end for end in ends if end != -1), default=len(text)) - i
    return 0


def strip_invisible_outside_literals(text: str, before: str = '') -> str:
    """Replace invisible characters, leaving quoted literals byte for byte.

    Inside `'...'`, `"..."` and backticks the characters are data, so rewriting
    them would silently corrupt a value or an identifier. An unterminated quote
    protects the remainder of the text, which errs towards changing nothing.

    Comments are not data, so they are cleaned like code; a quote inside one
    (`-- don't`) must not be taken for the start of a literal.

    `before` is the text that `text` will follow, such as the buffer up to the
    cursor for a paste. It only sets the state `text` starts in (inside a
    literal or a comment), and is not part of the result.
    """
    source = before + text
    offset = len(before)
    out: list[str] = []

    def emit(start: int, end: int, clean: bool) -> None:
        start = max(start, offset)
        if start < end:
            piece = source[start:end]
            out.append(piece.translate(TRANSLATIONS) if clean else piece)

    quote: str | None = None
    i = 0
    while i < len(source):
        char = source[i]

        if quote is None:
            if comment_length := _comment_length(source, i):
                emit(i, i + comment_length, True)
                i += comment_length
                continue
            if char in QUOTES:
                quote = char
                emit(i, i + 1, False)
            else:
                emit(i, i + 1, True)
            i += 1
            continue

        # A backslash escapes the next character in strings, but not in
        # backtick-quoted identifiers.
        if char == '\\' and quote != '`' and i + 1 < len(source):
            emit(i, i + 2, False)
            i += 2
            continue

        # A doubled quote is an escaped quote, not the end of the literal.
        if char == quote and i + 1 < len(source) and source[i + 1] == quote:
            emit(i, i + 2, False)
            i += 2
            continue

        if char == quote:
            quote = None
        emit(i, i + 1, False)
        i += 1

    return ''.join(out)
