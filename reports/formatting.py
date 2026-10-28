"""Форматирование значений для отчётов (русская и английская локали)."""
from __future__ import annotations

import datetime as dt
import decimal
import math
import re
import uuid
from decimal import Decimal

NBSP = "\u00a0"
_ID_NAME = re.compile(r"(^|[_\s])(id|guid|uuid|key|код|номер|number|num|no|год|year)($|[_\s])|id$", re.I)
MAX_CELL_CHARS = 2000
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")


def clean_xml_text(s: str) -> str:
    """Убирает символы, недопустимые в XML (иначе Word-документ нельзя создать/открыть)."""
    return _XML_BAD.sub("", s)


def looks_like_identifier(name: str) -> bool:
    """Столбцы-идентификаторы/годы: для них не нужны суммы и разделители тысяч."""
    return bool(_ID_NAME.search(name or ""))


class Formatter:
    def __init__(self, locale: str = "ru", currency_symbol: str = ""):
        self.ru = locale == "ru"
        self.currency = currency_symbol
        self.yes, self.no = ("Да", "Нет") if self.ru else ("Yes", "No")

    # --- числа ----------------------------------------------------------
    def number(self, value, decimals: int | None = None, grouping: bool = True) -> str:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        if isinstance(value, float):
            d = Decimal(f"{value:.6f}")
            if decimals is None:
                frac = f"{value:.6f}".rstrip("0").rstrip(".").partition(".")[2]
                decimals = len(frac)
        else:
            d = value if isinstance(value, Decimal) else Decimal(value)
            if decimals is None:
                decimals = min(6, max(0, -d.as_tuple().exponent))
        q = d.quantize(Decimal(1).scaleb(-decimals), rounding=decimal.ROUND_HALF_UP)
        text = f"{abs(q):,.{decimals}f}"
        if self.ru:
            text = text.replace(",", NBSP).replace(".", ",")
            if not grouping:
                text = text.replace(NBSP, "")
        elif not grouping:
            text = text.replace(",", "")
        if q < 0:
            text = ("−" if self.ru else "-") + text
        return text

    def money(self, value, decimals: int = 2) -> str:
        s = self.number(value, decimals)
        return f"{s}{NBSP}{self.currency}" if self.currency else s

    def percent(self, value, decimals: int = 1) -> str:
        text = self.number(Decimal(str(value)) * 100, decimals)
        return text + (NBSP + "%" if self.ru else "%")

    # --- даты ------------------------------------------------------------
    def date(self, value) -> str:
        return value.strftime("%d.%m.%Y") if self.ru else value.strftime("%Y-%m-%d")

    def datetime(self, value: dt.datetime) -> str:
        base = value.strftime("%d.%m.%Y %H:%M") if self.ru else value.strftime("%Y-%m-%d %H:%M")
        if value.second or value.microsecond:
            base += f":{value.second:02d}"
        return base

    # --- универсально ------------------------------------------------------
    def value(self, v, fmt: str = "auto", decimals: int = -1, *, grouping: bool = True,
              dates_only: bool = False) -> str:
        """Текст ячейки. None -> пустая строка (пустоту выделяет рендерер)."""
        if v is None:
            return ""
        dec = None if decimals < 0 else decimals
        if isinstance(v, bool):
            return self.yes if v else self.no
        if fmt in ("integer", "decimal", "currency", "percent") and isinstance(v, (int, float, Decimal)):
            if fmt == "integer":
                return self.number(round(v) if not isinstance(v, int) else v, 0, grouping)
            if fmt == "decimal":
                return self.number(v, 2 if dec is None else dec, grouping)
            if fmt == "currency":
                return self.money(v, 2 if dec is None else dec)
            return self.percent(v, 1 if dec is None else dec)
        if isinstance(v, dt.datetime):
            return self.date(v) if (fmt == "date" or dates_only) else self.datetime(v)
        if isinstance(v, dt.date):
            return self.date(v)
        if isinstance(v, dt.time):
            return v.strftime("%H:%M:%S")
        if isinstance(v, (int, float, Decimal)):
            return self.number(v, dec, grouping)
        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)
            return "0x" + b[:16].hex().upper() + ("…" if len(b) > 16 else "")
        if isinstance(v, uuid.UUID):
            return str(v).upper()
        s = clean_xml_text(str(v))
        return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "…"
