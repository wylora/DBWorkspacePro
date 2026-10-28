"""Локальная диагностика синтаксиса и привязка сообщений сервера к месту в тексте."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .sql_guard import split_batches
from .sql_lexer import BLOCK, BRACKET, DQUOTE, STRING, lex


@dataclass(frozen=True)
class Issue:
    start: int
    end: int
    message: str


_OPEN_MESSAGES = {STRING: "Не закрыта строка (нет завершающего ')",
                  BRACKET: "Не закрыт идентификатор в [ ]", DQUOTE: 'Не закрыт идентификатор в " "'}


def find_syntax_issues(sql: str) -> list[Issue]:
    """Незакрытые строки, комментарии, идентификаторы и несбалансированные скобки (по каждому пакету GO)."""
    issues: list[Issue] = []
    batches, _ = split_batches(sql)
    for b in batches:
        res = lex(b.text)
        if res.end_state != 0 and res.open_start is not None:
            msg = "Не закрыт комментарий /* */" if res.end_state >= BLOCK else _OPEN_MESSAGES[res.end_state]
            s = b.start_offset + res.open_start
            issues.append(Issue(s, min(s + 1, len(sql)), msg))
            continue
        stack: list[int] = []
        for t in res.tokens:
            if t.kind != "op":
                continue
            if t.text == "(":
                stack.append(t.start)
            elif t.text == ")":
                if stack:
                    stack.pop()
                else:
                    s = b.start_offset + t.start
                    issues.append(Issue(s, s + 1, "Лишняя закрывающая скобка"))
        for pos in stack:
            s = b.start_offset + pos
            issues.append(Issue(s, s + 1, "Не закрыта скобка"))
    return issues


_PATTERNS = [
    re.compile(r"near ['\"]([^'\"]+)['\"]", re.I),
    re.compile(r"около\s+(?:конструкции|ключевого слова|ключевое слово)?\s*['\"«]([^'\"»]+)['\"»]", re.I),
    re.compile(r"Invalid (?:object|column) name ['\"]([^'\"]+)['\"]", re.I),
    re.compile(r"Недопустимое имя (?:объекта|столбца)\s*['\"«]([^'\"»]+)['\"»]", re.I),
]
_LINE = re.compile(r"(?:\bLine|Строка)\s+(\d+)", re.I)


def locate_error(sql: str, message: str) -> tuple[int, int] | None:
    """Приблизительное место ошибки по тексту сообщения сервера (None, если определить нельзя).

    SQL Server через ODBC не сообщает позицию ошибки, поэтому ищем упомянутую в сообщении
    конструкцию среди слов кода (не в комментариях и строках).
    """
    words = [t for t in lex(sql).tokens if t.kind in ("word", "ident", "var", "number", "op")]
    for pat in _PATTERNS:
        m = pat.search(message or "")
        if not m:
            continue
        needle = m.group(1).strip().lower()
        last = needle.split(".")[-1].strip("[]\"")
        for t in words:
            tt = t.text.lower().strip("[]\"")
            if tt == needle or tt == last:
                return t.start, t.end
        idx = sql.lower().find(needle)
        if idx >= 0:
            return idx, idx + len(needle)
    m = _LINE.search(message or "")
    if m:
        line = int(m.group(1))
        lines = sql.split("\n")
        if 1 <= line <= len(lines):
            start = sum(len(x) + 1 for x in lines[:line - 1])
            return start, start + max(1, len(lines[line - 1]))
    return None
