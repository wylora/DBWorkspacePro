"""Статистика по реальным данным. Для неподходящих типов показатели не рассчитываются."""
from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from utils.formatting import sort_key
from .formatting import Formatter, looks_like_identifier
from .model import Metric

NUMERIC, INTEGER, DATE, DATETIME, TEXT, BOOL, BINARY, EMPTY, MIXED = (
    "numeric", "integer", "date", "datetime", "text", "bool", "binary", "empty", "mixed")


def _is_num(v) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) and \
        not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def infer_kind(values) -> str:
    kinds = set()
    all_midnight = True
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            kinds.add(BOOL)
        elif isinstance(v, int):
            kinds.add(INTEGER)
        elif isinstance(v, (float, Decimal)):
            kinds.add(NUMERIC)
        elif isinstance(v, dt.datetime):
            kinds.add(DATETIME)
            all_midnight = all_midnight and v.time() == dt.time(0)
        elif isinstance(v, dt.date):
            kinds.add(DATE)
        elif isinstance(v, (bytes, bytearray, memoryview)):
            kinds.add(BINARY)
        elif isinstance(v, str):
            kinds.add(TEXT)
        else:
            kinds.add(MIXED)
    if not kinds:
        return EMPTY
    if kinds <= {INTEGER, NUMERIC}:
        return INTEGER if kinds == {INTEGER} else NUMERIC
    if kinds <= {DATE, DATETIME}:
        return DATE if (kinds == {DATE} or all_midnight) else DATETIME
    return kinds.pop() if len(kinds) == 1 else MIXED


def exact_sum(values: list) -> Any:
    if any(isinstance(v, float) for v in values):
        return float(sum(float(v) for v in values))
    return sum(values, Decimal(0)) if any(isinstance(v, Decimal) for v in values) else sum(values)


@dataclass
class ColumnStats:
    name: str
    kind: str
    count: int = 0                  # непустых значений
    nulls: int = 0
    distinct: int = 0
    total: Any = None
    mean: Any = None
    median: Any = None
    minimum: Any = None
    maximum: Any = None
    top: list[tuple[Any, int]] = field(default_factory=list)
    skipped_reason: str = ""


def column_stats(name: str, values: list, *, skip_identifier_sums: bool = True) -> ColumnStats:
    kind = infer_kind(values)
    nn = [v for v in values if v is not None]
    cs = ColumnStats(name, kind, count=len(nn), nulls=len(values) - len(nn))
    if not nn:
        return cs
    try:
        cs.distinct = len(set(nn))
    except TypeError:
        cs.distinct = len({repr(v) for v in nn})
    if kind in (NUMERIC, INTEGER):
        nums = [v for v in nn if _is_num(v)]
        cs.minimum, cs.maximum = min(nums), max(nums)
        if skip_identifier_sums and looks_like_identifier(name):
            cs.skipped_reason = "идентификатор/год: сумма и среднее не имеют смысла"
        else:
            cs.total = exact_sum(nums)
            cs.mean = (cs.total / len(nums)) if not isinstance(cs.total, Decimal) else cs.total / Decimal(len(nums))
            cs.median = statistics.median([float(x) for x in nums])
    elif kind in (DATE, DATETIME):
        cs.minimum, cs.maximum = min(nn), max(nn)
    elif kind in (TEXT, BOOL):
        cs.top = Counter(nn).most_common(5)
    return cs


def dataset_stats(columns: list[str], rows: list[tuple], only: list[str] | None = None) -> list[ColumnStats]:
    out = []
    for i, name in enumerate(columns):
        if only and name not in only:
            continue
        out.append(column_stats(name, [r[i] for r in rows]))
    return out


# ------------------------------------------------------------ агрегации
def _agg(values: list, func: str):
    nums = [v for v in values if _is_num(v)]
    if func == "count":
        return len(values)
    if not nums:
        return None
    if func == "sum":
        return exact_sum(nums)
    if func == "avg":
        s = exact_sum(nums)
        return s / len(nums) if not isinstance(s, Decimal) else s / Decimal(len(nums))
    return min(nums) if func == "min" else max(nums)


def aggregate_by(rows: list[tuple], cat_idx: int, val_idx: int | None, func: str) -> list[tuple[Any, Any]]:
    groups: dict[Any, list] = {}
    for r in rows:
        key = r[cat_idx]
        if key is None:
            key = "(пусто)"
        groups.setdefault(key, []).append(r[val_idx] if val_idx is not None else 1)
    return [(k, _agg(v, func)) for k, v in groups.items()]


def period_of(d: dt.date, span_days: int) -> tuple[str, dt.date]:
    if span_days > 730:
        return f"{d.year}", dt.date(d.year, 1, 1)
    if span_days > 62:
        return f"{d.year}-{d.month:02d}", dt.date(d.year, d.month, 1)
    return d.isoformat(), dt.date(d.year, d.month, d.day)


def time_series(rows: list[tuple], date_idx: int, val_idx: int | None, func: str) -> list[tuple[str, Any]]:
    pts = [(r[date_idx].date() if isinstance(r[date_idx], dt.datetime) else r[date_idx],
            r[val_idx] if val_idx is not None else 1) for r in rows if isinstance(r[date_idx], dt.date)]
    if not pts:
        return []
    span = (max(p[0] for p in pts) - min(p[0] for p in pts)).days
    buckets: dict[dt.date, tuple[str, list]] = {}
    for d, v in pts:
        label, key = period_of(d, span)
        buckets.setdefault(key, (label, []))[1].append(v)
    return [(buckets[k][0], _agg(buckets[k][1], func)) for k in sorted(buckets)]


def metric_value(columns: list[str], rows: list[tuple], m: Metric):
    idx = {c: i for i, c in enumerate(columns)}
    if m.column not in idx:
        return None
    vals = [r[idx[m.column]] for r in rows]
    nn = [v for v in vals if v is not None]
    if m.op == "count":
        return len(nn)
    if m.op == "distinct":
        return len(set(nn))
    if m.op == "ratio":
        if m.column2 not in idx:
            return None
        a = _agg(vals, "sum")
        b = _agg([r[idx[m.column2]] for r in rows], "sum")
        if a is None or not b:
            return None
        return Decimal(str(a)) / Decimal(str(b))
    return _agg(vals, m.op)


# ------------------------------------------------------------- выводы
def conclusions(name: str, columns: list[str], rows: list[tuple], truncated: bool, fmt: Formatter,
                limit: int = 8) -> list[str]:
    """Только проверяемые факты из данных; причинно-следственных утверждений не делаем."""
    out = [f"Источник «{name}»: получено записей — {fmt.number(len(rows), 0)}."]
    if truncated:
        out.append("Данные получены не полностью (достигнут лимит строк), поэтому все показатели относятся "
                   "только к полученной части.")
    if not rows:
        return out
    stats = dataset_stats(columns, rows)
    dates = next((s for s in stats if s.kind in (DATE, DATETIME) and s.count), None)
    if dates:
        out.append(f"Период данных по полю «{dates.name}»: с {_fmt_val(fmt, dates.minimum)} "
                   f"по {_fmt_val(fmt, dates.maximum)}.")
    for s in stats:
        if len(out) >= limit:
            break
        if s.kind in (NUMERIC, INTEGER) and s.total is not None and s.count:
            out.append(f"«{s.name}»: сумма — {fmt.number(s.total)}, среднее — {fmt.number(s.mean, 2)}, "
                       f"минимум — {fmt.number(s.minimum)}, максимум — {fmt.number(s.maximum)}.")
        elif s.kind == TEXT and s.top and s.distinct > 1:
            val, cnt = s.top[0]
            share = cnt / s.count * 100
            out.append(f"«{s.name}»: {fmt.number(s.distinct, 0)} уникальных значений; наиболее частое — "
                       f"«{val}» ({fmt.number(cnt, 0)} зап., {fmt.number(Decimal(str(round(share, 1))), 1)}%).")
        if s.nulls and s.count + s.nulls and s.nulls / (s.count + s.nulls) >= 0.2 and len(out) < limit:
            out.append(f"В поле «{s.name}» не заполнено {fmt.number(s.nulls, 0)} из "
                       f"{fmt.number(s.count + s.nulls, 0)} значений.")
    return out[:limit]


def _fmt_val(fmt: Formatter, v) -> str:
    return fmt.value(v, "date") if isinstance(v, (dt.date, dt.datetime)) else str(v)
