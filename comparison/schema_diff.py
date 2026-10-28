"""Сравнение структуры двух баз (или выбранных таблиц).

Ограничения и индексы сопоставляются по сути (столбцы, связанная таблица, уникальность), а не по имени:
имена, сгенерированные сервером, в разных базах почти всегда различаются.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import Column, ForeignKey, Index, Module, SchemaModel, Table

CATEGORY_RU = {"table": "Таблица", "column": "Столбец", "pk": "Первичный ключ", "fk": "Внешний ключ", "index": "Индекс",
               "view": "Представление", "procedure": "Процедура", "function": "Функция", "trigger": "Триггер"}
STATUS_RU = {"only_source": "только в исходной", "only_target": "только в целевой", "changed": "различается",
             "unknown": "сравнить нельзя"}


@dataclass
class Difference:
    category: str
    object: str
    status: str
    details: list[str] = field(default_factory=list)
    table: str = ""
    source: object = None
    target: object = None


@dataclass
class SchemaDiff:
    source: SchemaModel
    target: SchemaModel
    items: list[Difference] = field(default_factory=list)
    equal_tables: int = 0
    equal_modules: int = 0
    notes: list[str] = field(default_factory=list)

    def count(self, status: str | None = None, category: str | None = None) -> int:
        return sum(1 for d in self.items if (status is None or d.status == status) and (category is None or d.category == category))

    @property
    def identical(self) -> bool:
        return not any(d.status != "unknown" for d in self.items)

    def summary(self) -> str:
        if self.identical:
            return "Различий в структуре не найдено" + (" (часть определений сравнить нельзя)" if self.items else "") + "."
        parts = [f"{STATUS_RU[s]}: {self.count(s)}" for s in ("only_source", "only_target", "changed", "unknown") if self.count(s)]
        return "Различий: " + str(sum(1 for d in self.items if d.status != "unknown")) + " (" + "; ".join(parts) + ")"


def _norm_type(t: str) -> str:
    return t.replace(" ", "").casefold()


def _norm_default(d: str) -> str:
    s = re.sub(r"\s+", "", d or "").casefold()
    while s.startswith("(") and s.endswith(")") and _balanced(s[1:-1]):
        s = s[1:-1]
    return s


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        depth += (ch == "(") - (ch == ")")
        if depth < 0:
            return False
    return depth == 0


def normalize_definition(text: str, ignore_whitespace: bool = True) -> str:
    t = (text or "").replace("\r\n", "\n").strip()
    if ignore_whitespace:
        t = re.sub(r"\s+", " ", t)
    return t


def _column_changes(a: Column, b: Column, ignore_collation: bool) -> list[str]:
    out = []
    if _norm_type(a.type) != _norm_type(b.type):
        out.append(f"тип: {b.type} → {a.type}")
    if a.nullable != b.nullable:
        out.append(f"NULL: {'да' if b.nullable else 'нет'} → {'да' if a.nullable else 'нет'}")
    if a.identity != b.identity or (a.identity and (a.seed, a.increment) != (b.seed, b.increment)):
        out.append(f"IDENTITY: {_ident(b)} → {_ident(a)}")
    if _norm_default(a.computed) != _norm_default(b.computed):
        out.append(f"вычисляемое выражение: {b.computed or '—'} → {a.computed or '—'}")
    if _norm_default(a.default) != _norm_default(b.default):
        out.append(f"значение по умолчанию: {b.default or '—'} → {a.default or '—'}")
    if not ignore_collation and (a.collation or "").casefold() != (b.collation or "").casefold():
        out.append(f"сортировка: {b.collation or '—'} → {a.collation or '—'}")
    return out


def _ident(c: Column) -> str:
    return f"({c.seed},{c.increment})" if c.identity else "нет"


def _cols(xs: list[str]) -> tuple:
    return tuple(x.casefold() for x in xs)


def _index_sig(i: Index) -> tuple:
    return (i.type_desc.casefold(), i.unique, _cols(i.columns), tuple(sorted(_cols(i.included))),
            re.sub(r"\s+", "", i.filter or "").casefold())


def _fk_sig(f: ForeignKey) -> tuple:
    return (_cols(f.columns), f.ref_schema.casefold(), f.ref_table.casefold(), _cols(f.ref_columns))


def _pair(src_list, tgt_list, sig, name=lambda x: x.name.casefold()):
    """Пары (исх, цел) сначала по имени, затем по сути; плюс непарные остатки."""
    pairs, left_t = [], list(tgt_list)
    rest_s = []
    for a in src_list:
        b = next((x for x in left_t if name(x) == name(a)), None)
        if b is not None:
            pairs.append((a, b))
            left_t.remove(b)
        else:
            rest_s.append(a)
    only_s = []
    for a in rest_s:
        b = next((x for x in left_t if sig(x) == sig(a)), None)
        if b is not None:
            pairs.append((a, b))
            left_t.remove(b)
        else:
            only_s.append(a)
    return pairs, only_s, left_t


def compare_schemas(src: SchemaModel, tgt: SchemaModel, *, compare_definitions: bool = True,
                    ignore_whitespace: bool = True, ignore_collation: bool = False, compare_names: bool = False) -> SchemaDiff:
    d = SchemaDiff(src, tgt)
    add = d.items.append
    for key, a in sorted(src.tables.items()):
        b = tgt.tables.get(key)
        if b is None:
            add(Difference("table", a.full, "only_source", [f"столбцов: {len(a.columns)}"], a.full, a, None))
            continue
        before = len(d.items)
        _compare_table(a, b, d, ignore_collation, compare_names)
        if len(d.items) == before:
            d.equal_tables += 1
    for key, b in sorted(tgt.tables.items()):
        if key not in src.tables:
            add(Difference("table", b.full, "only_target", [f"столбцов: {len(b.columns)}"], b.full, None, b))
    for key, a in sorted(src.modules.items()):
        b = tgt.modules.get(key)
        if b is None:
            add(Difference(a.kind, a.full, "only_source", [], a.parent, a, None))
            continue
        if not compare_definitions:
            d.equal_modules += 1
            continue
        if a.definition is None or b.definition is None:
            side = "исходной" if a.definition is None else "целевой"
            add(Difference(a.kind, a.full, "unknown", [f"определение недоступно в {side} базе (нет права VIEW DEFINITION "
                                                       "или объект зашифрован)"], a.parent, a, b))
        elif normalize_definition(a.definition, ignore_whitespace) != normalize_definition(b.definition, ignore_whitespace):
            la, lb = len(a.definition.splitlines()), len(b.definition.splitlines())
            add(Difference(a.kind, a.full, "changed", [f"определение различается (строк: {lb} → {la})"], a.parent, a, b))
        else:
            d.equal_modules += 1
    for key, b in sorted(tgt.modules.items()):
        if key not in src.modules:
            add(Difference(b.kind, b.full, "only_target", [], b.parent, None, b))
    if src.warnings or tgt.warnings:
        d.notes += [f"Исходная база: {w}" for w in src.warnings] + [f"Целевая база: {w}" for w in tgt.warnings]
    return d


def _compare_table(a: Table, b: Table, d: SchemaDiff, ignore_collation: bool, compare_names: bool) -> None:
    add = d.items.append
    bc = {c.name.casefold(): c for c in b.columns}
    ac = {c.name.casefold(): c for c in a.columns}
    for c in a.columns:
        o = bc.get(c.name.casefold())
        if o is None:
            add(Difference("column", f"{a.full}.{c.name}", "only_source", [f"{c.type}, {'NULL' if c.nullable else 'NOT NULL'}"],
                           a.full, c, None))
        else:
            ch = _column_changes(c, o, ignore_collation)
            if ch:
                add(Difference("column", f"{a.full}.{c.name}", "changed", ch, a.full, c, o))
    for c in b.columns:
        if c.name.casefold() not in ac:
            add(Difference("column", f"{a.full}.{c.name}", "only_target", [c.type], a.full, None, c))
    pa, pb = a.pk, b.pk
    if pa and not pb:
        add(Difference("pk", f"{a.full}", "only_source", [", ".join(pa.columns)], a.full, pa, None))
    elif pb and not pa:
        add(Difference("pk", f"{a.full}", "only_target", [", ".join(pb.columns)], a.full, None, pb))
    elif pa and pb:
        det = []
        if _cols(pa.columns) != _cols(pb.columns):
            det.append(f"столбцы: {', '.join(pb.columns)} → {', '.join(pa.columns)}")
        if pa.type_desc != pb.type_desc:
            det.append(f"вид: {pb.type_desc} → {pa.type_desc}")
        if compare_names and pa.name.casefold() != pb.name.casefold():
            det.append(f"имя: {pb.name} → {pa.name}")
        if det:
            add(Difference("pk", a.full, "changed", det, a.full, pa, pb))
    ia = [i for i in a.indexes if not i.primary]
    ib = [i for i in b.indexes if not i.primary]
    pairs, only_s, only_t = _pair(ia, ib, _index_sig)
    for x, y in pairs:
        det = []
        if _index_sig(x) != _index_sig(y):
            det.append(f"определение: ({', '.join(y.columns)}) → ({', '.join(x.columns)})"
                       + ("; уникальность" if x.unique != y.unique else "")
                       + ("; INCLUDE" if _cols(x.included) != _cols(y.included) else "")
                       + ("; фильтр" if (x.filter or "") != (y.filter or "") else ""))
        if compare_names and x.name.casefold() != y.name.casefold():
            det.append(f"имя: {y.name} → {x.name}")
        if det:
            add(Difference("index", f"{a.full}: {x.name}", "changed", det, a.full, x, y))
    for x in only_s:
        add(Difference("index", f"{a.full}: {x.name}", "only_source", [f"({', '.join(x.columns)})"], a.full, x, None))
    for y in only_t:
        add(Difference("index", f"{a.full}: {y.name}", "only_target", [f"({', '.join(y.columns)})"], a.full, None, y))
    pairs, only_s, only_t = _pair(a.fks, b.fks, _fk_sig)
    for x, y in pairs:
        det = []
        if _fk_sig(x) != _fk_sig(y):
            det.append(f"связь: → {y.ref_schema}.{y.ref_table}({', '.join(y.ref_columns)}) стало "
                       f"→ {x.ref_schema}.{x.ref_table}({', '.join(x.ref_columns)})")
        if (x.on_delete, x.on_update) != (y.on_delete, y.on_update):
            det.append(f"каскадные действия: {y.on_delete}/{y.on_update} → {x.on_delete}/{x.on_update}")
        if x.disabled != y.disabled:
            det.append("ограничение " + ("отключено" if x.disabled else "включено") + " в исходной базе")
        if det:
            add(Difference("fk", f"{a.full}: {x.name}", "changed", det, a.full, x, y))
    for x in only_s:
        add(Difference("fk", f"{a.full}: {x.name}", "only_source", [f"→ {x.ref_schema}.{x.ref_table}"], a.full, x, None))
    for y in only_t:
        add(Difference("fk", f"{a.full}: {y.name}", "only_target", [f"→ {y.ref_schema}.{y.ref_table}"], a.full, None, y))
