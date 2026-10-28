"""Сравнение двух наборов строк по ключевым столбцам: новые, удалённые, изменённые записи и итоги.

Если ключ неуникален, надёжно сопоставить записи нельзя — сравнение по строкам не выполняется (DiffError).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


class DiffError(ValueError):
    pass


def norm(v):
    """Приводит значения к сравнимому виду (1 == 1.0 == Decimal('1.00'), дата == её ISO-строка и т. п.)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, Decimal)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return str(v)
        try:
            d = Decimal(str(v)).normalize()
            return d if d != 0 else Decimal(0)
        except InvalidOperation:
            return str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "0x" + bytes(v).hex().upper()
    if isinstance(v, str):
        s = v.strip()
        try:                                    # число, сохранённое текстом в снимке (JSON для Decimal)
            if s and (s[0].isdigit() or s[0] in "-+") and s.replace(".", "", 1).lstrip("+-").isdigit():
                return Decimal(s).normalize()
        except InvalidOperation:
            pass
        return v
    return str(v)


@dataclass
class RowChange:
    key: tuple
    changes: dict[str, tuple]               # столбец -> (было, стало)


@dataclass
class DataDiff:
    key_columns: list[str]
    compared_columns: list[str]
    old_count: int
    new_count: int
    added: list[tuple] = field(default_factory=list)
    removed: list[tuple] = field(default_factory=list)
    changed: list[RowChange] = field(default_factory=list)
    unchanged: int = 0
    added_total: int = 0
    removed_total: int = 0
    changed_total: int = 0
    columns_added: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)
    totals: dict[str, tuple] = field(default_factory=dict)    # числовой столбец -> (сумма было, сумма стало)

    @property
    def has_changes(self) -> bool:
        return bool(self.added_total or self.removed_total or self.changed_total or self.columns_added
                    or self.columns_removed)

    def summary(self) -> str:
        parts = [f"новых записей: {self.added_total}", f"удалённых: {self.removed_total}",
                 f"изменённых: {self.changed_total}", f"без изменений: {self.unchanged}"]
        if self.columns_added:
            parts.append("новые столбцы: " + ", ".join(self.columns_added))
        if self.columns_removed:
            parts.append("исчезнувшие столбцы: " + ", ".join(self.columns_removed))
        for col, (a, b) in self.totals.items():
            if a != b:
                parts.append(f"сумма «{col}»: {a} → {b}")
        return "; ".join(parts)


def totals_diff(old_cols, old_rows, new_cols, new_rows, exclude=()) -> dict[str, tuple]:
    from reports.formatting import looks_like_identifier
    out = {}
    for col in [c for c in new_cols if c in old_cols and c not in exclude and not looks_like_identifier(c)]:
        oi, ni = old_cols.index(col), new_cols.index(col)
        ov = [norm(r[oi]) for r in old_rows]
        nv = [norm(r[ni]) for r in new_rows]
        if all(isinstance(x, Decimal) or x is None for x in ov + nv) and any(x is not None for x in ov + nv):
            out[col] = (sum((x for x in ov if x is not None), Decimal(0)), sum((x for x in nv if x is not None), Decimal(0)))
    return out


def diff_tables(old_cols: list[str], old_rows: list[tuple], new_cols: list[str], new_rows: list[tuple],
                key_columns: list[str], compare_columns: list[str] | None = None, max_items: int = 1000,
                key_casefold: bool = False) -> DataDiff:
    if not key_columns:
        raise DiffError("Не задан ключ для сопоставления записей.")
    missing = [k for k in key_columns if k not in old_cols or k not in new_cols]
    if missing:
        raise DiffError("Ключевых столбцов нет в обоих наборах: " + ", ".join(missing))
    common = [c for c in new_cols if c in old_cols and c not in key_columns]
    cmp_cols = [c for c in (compare_columns or common) if c in common]
    ok_idx = [old_cols.index(k) for k in key_columns]
    nk_idx = [new_cols.index(k) for k in key_columns]

    def index(rows, kidx, label):
        out: dict[tuple, tuple] = {}
        dups = 0
        for r in rows:
            k = tuple((norm(r[i]).casefold() if key_casefold and isinstance(r[i], str) else norm(r[i])) for i in kidx)
            if k in out:
                dups += 1
            out[k] = r
        if dups:
            raise DiffError(f"Ключ ({', '.join(key_columns)}) неуникален в {label} наборе: повторов — {dups}. "
                            "Надёжно сопоставить записи нельзя; выберите другой ключ.")
        return out

    old_map = index(old_rows, ok_idx, "прежнем")
    new_map = index(new_rows, nk_idx, "новом")
    d = DataDiff(list(key_columns), cmp_cols, len(old_rows), len(new_rows),
                 columns_added=[c for c in new_cols if c not in old_cols],
                 columns_removed=[c for c in old_cols if c not in new_cols])
    pairs = [(old_cols.index(c), new_cols.index(c), c) for c in cmp_cols]
    for k, nr in new_map.items():  # noqa: B007
        orow = old_map.get(k)
        if orow is None:
            d.added_total += 1
            if len(d.added) < max_items:
                d.added.append(nr)
            continue
        changes = {c: (orow[oi], nr[ni]) for oi, ni, c in pairs if norm(orow[oi]) != norm(nr[ni])}
        if changes:
            d.changed_total += 1
            if len(d.changed) < max_items:
                d.changed.append(RowChange(tuple(nr[i] for i in nk_idx), changes))
        else:
            d.unchanged += 1
    for k, orow in old_map.items():
        if k not in new_map:
            d.removed_total += 1
            if len(d.removed) < max_items:
                d.removed.append(orow)
    d.totals = totals_diff(old_cols, old_rows, new_cols, new_rows, exclude=key_columns)
    return d
