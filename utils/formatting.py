"""Отображение значений ячеек и безопасная сортировка разнородных значений."""
from __future__ import annotations

import datetime as dt
import decimal
import math
import uuid

NULL_TEXT = "NULL"


def format_cell(value, max_len: int = 500) -> str:
    if value is None:
        return NULL_TEXT
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, dt.datetime):
        s = value.strftime("%Y-%m-%d %H:%M:%S")
        if value.microsecond:
            s += f".{value.microsecond:06d}".rstrip("0")
        return s + (value.strftime(" %z") if value.tzinfo else "")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        return "0x" + b[:32].hex().upper() + ("…" if len(b) > 32 else "")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return repr(value)
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value).upper()
    s = str(value)
    return s if len(s) <= max_len else s[:max_len] + "…"


def sort_key(value):
    """Ключ сортировки: NULL последними; числа, даты, строки, остальное — раздельными группами."""
    if value is None:
        return (9, 0)
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float, decimal.Decimal)):
        if isinstance(value, float) and math.isnan(value):
            return (8, 0)
        return (0, value)
    if isinstance(value, dt.datetime):
        return (1, value.replace(tzinfo=None).timestamp() if value.year > 1970 else value.replace(tzinfo=None).toordinal())
    if isinstance(value, dt.date):
        return (1, dt.datetime(value.year, value.month, value.day).timestamp() if value.year > 1970 else value.toordinal())
    if isinstance(value, str):
        return (2, value.lower())
    if isinstance(value, (bytes, bytearray)):
        return (3, bytes(value))
    return (4, str(value))


def is_numeric(value) -> bool:
    return isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool)
