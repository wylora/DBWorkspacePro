"""Построчный лексер T-SQL: единая основа для подсветки, разбора GO, защиты и диагностики.

Состояние между строками (для QSyntaxHighlighter.setCurrentBlockState) — целое число:
0 — обычный код; 1 — внутри '...'; 2 — внутри [...]; 3 — внутри "..."; 10+N — комментарий /* */ глубины N.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NORMAL, STRING, BRACKET, DQUOTE = 0, 1, 2, 3
BLOCK = 10

_NUM = re.compile(r"0[xX][0-9A-Fa-f]*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?")
_WORD = re.compile(r"(?:#{1,2}|[^\W\d])[\w$#@]*")
_VAR = re.compile(r"@@?[\w$#@]*")
_QUOTES = {STRING: "'", BRACKET: "]", DQUOTE: '"'}


@dataclass(frozen=True)
class Token:
    kind: str      # word | number | string | ident | comment | op | var
    start: int
    end: int
    text: str
    terminated: bool = True

    @property
    def upper(self) -> str:
        return self.text.upper()


@dataclass
class LexResult:
    tokens: list[Token]
    end_state: int
    open_start: int | None = None   # начало незакрытой строки/комментария/идентификатора


def _close(line: str, i: int, quote: str) -> int:
    """Индекс после закрывающей кавычки или -1; удвоенная кавычка — экранирование."""
    n = len(line)
    while i < n:
        if line[i] == quote:
            if i + 1 < n and line[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def _scan_block(line: str, i: int, depth: int) -> tuple[int, int]:
    n = len(line)
    while i < n:
        if line.startswith("/*", i):
            depth += 1
            i += 2
        elif line.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i, 0
        else:
            i += 1
    return n, depth


def lex_line(line: str, state: int = NORMAL, base: int = 0) -> tuple[list[Token], int]:
    toks: list[Token] = []
    i, n = 0, len(line)
    while i < n:
        if state >= BLOCK:
            j, depth = _scan_block(line, i, state - BLOCK)
            toks.append(Token("comment", base + i, base + j, line[i:j], depth == 0))
            state = NORMAL if depth == 0 else BLOCK + depth
            i = j
            continue
        if state in _QUOTES:
            j = _close(line, i, _QUOTES[state])
            kind = "string" if state == STRING else "ident"
            if j < 0:
                toks.append(Token(kind, base + i, base + n, line[i:], False))
                i = n
            else:
                toks.append(Token(kind, base + i, base + j, line[i:j], True))
                state = NORMAL
                i = j
            continue
        c = line[i]
        if c.isspace():
            i += 1
        elif line.startswith("--", i):
            toks.append(Token("comment", base + i, base + n, line[i:]))
            i = n
        elif line.startswith("/*", i):
            j, depth = _scan_block(line, i + 2, 1)
            toks.append(Token("comment", base + i, base + j, line[i:j], depth == 0))
            state = NORMAL if depth == 0 else BLOCK + depth
            i = j
        elif c in ("'", "[", '"'):
            new_state = {"'": STRING, "[": BRACKET, '"': DQUOTE}[c]
            j = _close(line, i + 1, _QUOTES[new_state])
            kind = "string" if new_state == STRING else "ident"
            if j < 0:
                toks.append(Token(kind, base + i, base + n, line[i:], False))
                state, i = new_state, n
            else:
                toks.append(Token(kind, base + i, base + j, line[i:j], True))
                i = j
        elif c == "@":
            m = _VAR.match(line, i)
            toks.append(Token("var", base + i, base + m.end(), m.group()))
            i = m.end()
        elif c.isdigit():
            m = _NUM.match(line, i)
            toks.append(Token("number", base + i, base + m.end(), m.group()))
            i = m.end()
        else:
            m = _WORD.match(line, i)
            if m:
                toks.append(Token("word", base + i, base + m.end(), m.group()))
                i = m.end()
            else:
                toks.append(Token("op", base + i, base + i + 1, c))
                i += 1
    return toks, state


def lex(sql: str) -> LexResult:
    tokens: list[Token] = []
    state, offset = NORMAL, 0
    region: int | None = None
    for line in sql.split("\n"):
        before = state
        toks, state = lex_line(line, state, offset)
        tokens.extend(toks)
        if before == NORMAL and state != NORMAL and toks:
            region = toks[-1].start
        elif before != NORMAL and state == NORMAL:
            region = None
        offset += len(line) + 1
    return LexResult(tokens, state, region if state != NORMAL else None)
