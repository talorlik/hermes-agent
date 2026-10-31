"""Closed note-line rule for a dropped sync line.

A line is a note only when this module says so. Every other line stops.
Do not add a case.
"""

from __future__ import annotations

import io
import tokenize

_DIRECTIVES = frozenset({
    "include",
    "define",
    "undef",
    "if",
    "ifdef",
    "ifndef",
    "elif",
    "elifdef",
    "elifndef",
    "else",
    "endif",
    "pragma",
    "error",
    "warning",
    "line",
    "embed",
})

_NOTE_TYPES = frozenset({
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.ENDMARKER,
    tokenize.ENCODING,
    tokenize.INDENT,
    tokenize.DEDENT,
})


def is_note_line(
    line: str,
    *,
    language: str,
    source: str | None = None,
    line_number: int = 1,
) -> bool:
    """Return True only for a closed note. Every other line stops the sync."""
    if line.strip() == "":
        return True
    if language == "python":
        text = line if source is None else source
        number = 1 if source is None else line_number
        return _python_note(text, number)
    stripped = line.strip()
    if stripped.startswith("//"):
        return True
    if stripped.startswith("#"):
        rest = stripped[1:].lstrip()
        word = rest.split(None, 1)[0] if rest else ""
        return word not in _DIRECTIVES
    return False


def _python_note(source: str, line_number: int) -> bool:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return False
    starting = [tok for tok in tokens if tok.start[0] == line_number]
    if not starting:
        return _covered_by_docstring(tokens, line_number)
    for tok in starting:
        if tok.type in _NOTE_TYPES:
            continue
        if tok.type == tokenize.STRING and _is_docstring(tokens, tok):
            continue
        return False
    return True


def _covered_by_docstring(tokens: list[tokenize.TokenInfo], line_number: int) -> bool:
    for tok in tokens:
        if tok.type != tokenize.STRING or not _is_docstring(tokens, tok):
            continue
        if tok.start[0] <= line_number <= tok.end[0]:
            return True
    return False


def _is_docstring(tokens: list[tokenize.TokenInfo], tok: tokenize.TokenInfo) -> bool:
    index = next(i for i, item in enumerate(tokens) if item is tok)
    cursor = index - 1
    while cursor >= 0 and tokens[cursor].type in (tokenize.NL, tokenize.COMMENT):
        cursor -= 1
    if cursor < 0:
        return True
    return tokens[cursor].type in (tokenize.INDENT, tokenize.ENCODING)
