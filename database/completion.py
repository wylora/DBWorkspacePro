"""Автодополнение T-SQL (best-effort): ключевые слова, объекты базы, столбцы таблиц запроса."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sql_guard import split_batches
from .sql_keywords import COMPLETION_WORDS, DATATYPES, FUNCTIONS, KEYWORDS
from .sql_lexer import lex

KIND_LABELS = {"keyword": "ключевое слово", "function": "функция", "type": "тип", "table": "таблица",
               "view": "представление", "procedure": "процедура", "function_obj": "функция БД",
               "column": "столбец", "schema": "схема", "trigger": "триггер"}


@dataclass(frozen=True)
class ObjectEntry:
    schema: str
    name: str
    kind: str


@dataclass
class SchemaSnapshot:
    objects: list[ObjectEntry] = field(default_factory=list)
    columns: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    truncated: bool = False

    def find_table(self, name: str, schema: str | None = None) -> tuple[str, str] | None:
        low = name.lower()
        for (s, n) in self.columns:
            if n.lower() == low and (schema is None or s.lower() == schema.lower()):
                return s, n
        for o in self.objects:
            if o.name.lower() == low and (schema is None or o.schema.lower() == schema.lower()) \
                    and o.kind in ("table", "view"):
                return o.schema, o.name
        return None


@dataclass(frozen=True)
class Completion:
    text: str       # что показать
    insert: str     # что вставить
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class TableRef:
    alias: str
    schema: str
    name: str


_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_@#$]*$")


def quote_if_needed(name: str) -> str:
    if _PLAIN.match(name) and name.upper() not in KEYWORDS:
        return name
    return "[" + name.replace("]", "]]") + "]"


def _unq(text: str) -> str:
    return text.strip("[]\"") if text[:1] in "[\"" else text


def extract_table_refs(sql: str, pos: int) -> list[TableRef]:
    """Таблицы из FROM/JOIN/UPDATE/INTO/DELETE пакета, в котором стоит курсор (с псевдонимами)."""
    batches, _ = split_batches(sql)
    batch = next((b for b in batches if b.start_offset <= pos <= b.start_offset + len(b.text)),
                 batches[-1] if batches else None)
    if batch is None:
        return []
    toks = [t for t in lex(batch.text).tokens if t.kind != "comment"]
    refs: list[TableRef] = []
    triggers = {"FROM", "JOIN", "UPDATE", "INTO", "MERGE"}
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t.kind == "word" and t.upper in triggers:
            j = i + 1
            while j < n:  # допускаем список через запятую: FROM a x, b y
                parts: list[str] = []
                while j < n and toks[j].kind in ("word", "ident"):
                    if toks[j].kind == "word" and toks[j].upper in KEYWORDS and not parts:
                        break
                    parts.append(_unq(toks[j].text))
                    if j + 1 < n and toks[j + 1].kind == "op" and toks[j + 1].text == ".":
                        j += 2
                        continue
                    j += 1
                    break
                if not parts:
                    break
                alias = parts[-1]
                if j < n and toks[j].kind == "word" and toks[j].upper == "AS":
                    j += 1
                if j < n and toks[j].kind in ("word", "ident") and (
                        toks[j].kind == "ident" or toks[j].upper not in KEYWORDS):
                    alias = _unq(toks[j].text)
                    j += 1
                schema = parts[-2] if len(parts) >= 2 else ""
                refs.append(TableRef(alias, schema, parts[-1]))
                if j < n and toks[j].kind == "op" and toks[j].text == "," and t.upper == "FROM":
                    j += 1
                    continue
                break
            i = max(j, i + 1)
        else:
            i += 1
    return refs


_QUAL = re.compile(r"((?:\[[^\]]+\]|\w+)(?:\.(?:\[[^\]]+\]|\w+))?)\.$")
_WORD_CHARS = re.compile(r"[\w@#$]")


def prefix_start(text: str, pos: int) -> int:
    i = pos
    while i > 0 and _WORD_CHARS.match(text[i - 1]):
        i -= 1
    return i


def complete(sql: str, pos: int, snapshot: SchemaSnapshot | None, limit: int = 200) -> tuple[int, list[Completion]]:
    """(начало заменяемого префикса, варианты). Внутри строк и комментариев вариантов нет."""
    before = sql[:pos]
    res = lex(before)
    if res.end_state != 0:
        return pos, []
    if res.tokens and res.tokens[-1].kind == "comment" and res.tokens[-1].text.startswith("--") \
            and res.tokens[-1].end >= len(before):
        return pos, []          # курсор внутри строчного комментария
    start = prefix_start(sql, pos)
    prefix = sql[start:pos]
    snap = snapshot or SchemaSnapshot()
    items: list[Completion] = []
    low = prefix.lower()

    def ok(name: str) -> bool:
        return name.lower().startswith(low)

    m = _QUAL.search(sql[:start])
    if m:
        segs = [_unq(s) for s in m.group(1).split(".")]
        refs = extract_table_refs(sql, pos)
        target: tuple[str, str] | None = None
        if len(segs) == 1:
            q = segs[0].lower()
            for r in refs:
                if r.alias.lower() == q or r.name.lower() == q:
                    target = snap.find_table(r.name, r.schema or None)
                    break
            if target is None:
                if any(o.schema.lower() == q for o in snap.objects):
                    for o in snap.objects:
                        if o.schema.lower() == q and ok(o.name):
                            items.append(Completion(o.name, quote_if_needed(o.name), o.kind, o.schema))
                    return start, items[:limit]
                target = snap.find_table(segs[0])
        else:
            target = snap.find_table(segs[1], segs[0])
        if target is not None:
            for col, typ in snap.columns.get(target, []):
                if ok(col):
                    items.append(Completion(col, quote_if_needed(col), "column", typ))
        return start, items[:limit]

    if not prefix:
        return start, []

    seen: set[tuple[str, str]] = set()
    for r in extract_table_refs(sql, pos):
        tgt = snap.find_table(r.name, r.schema or None)
        for col, typ in snap.columns.get(tgt, []) if tgt else []:
            if ok(col) and (col.lower(), "column") not in seen:
                seen.add((col.lower(), "column"))
                items.append(Completion(col, quote_if_needed(col), "column", f"{typ} · {r.name}"))
    for o in snap.objects:
        if ok(o.name):
            ins = quote_if_needed(o.name) if o.schema.lower() in ("", "dbo") \
                else f"{quote_if_needed(o.schema)}.{quote_if_needed(o.name)}"
            kind = "function_obj" if o.kind == "function" else o.kind
            items.append(Completion(o.name, ins, kind, o.schema))
    for sch in sorted({o.schema for o in snap.objects if o.schema and o.schema.lower() != "dbo"}):
        if ok(sch):
            items.append(Completion(sch, quote_if_needed(sch), "schema"))
    for w in COMPLETION_WORDS:
        if ok(w) and w != prefix.upper():
            kind = "function" if w in FUNCTIONS else "type" if w in DATATYPES else "keyword"
            items.append(Completion(w, w, kind))
    return start, items[:limit]
