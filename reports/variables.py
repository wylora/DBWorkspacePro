"""Параметры SQL (:имя -> ?) и переменные шаблона ({{имя}})."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from database.sql_lexer import lex
from .model import Param

VAR_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class ParamError(ValueError):
    pass


def _named_param_spans(sql: str) -> list[tuple[int, int, str]]:
    toks = lex(sql).tokens
    spans = []
    for i, t in enumerate(toks):
        if t.kind != "op" or t.text != ":" or i + 1 >= len(toks):
            continue
        nxt = toks[i + 1]
        prev = toks[i - 1] if i else None
        if nxt.kind == "word" and nxt.start == t.end and not nxt.text.startswith("#"):
            if prev is not None and prev.kind == "op" and prev.text == ":" and prev.end == t.start:
                continue                       # «::» (geography::Point)
            if i + 2 < len(toks) and toks[i + 2].kind == "op" and toks[i + 2].text == ":" \
                    and toks[i + 2].start == nxt.end:
                continue
            spans.append((t.start, nxt.end, nxt.text))
    return spans


def find_named_params(sql: str) -> list[str]:
    out: list[str] = []
    for _s, _e, name in _named_param_spans(sql):
        if name not in out:
            out.append(name)
    return out


def convert_named_params(sql: str, values: dict[str, Any]) -> tuple[str, list[Any]]:
    """:имя -> ? с упорядоченным списком значений. Значения всегда передаются параметрами, не текстом."""
    spans = _named_param_spans(sql)
    params: list[Any] = []
    out, last = [], 0
    for start, end, name in spans:
        if name not in values:
            raise ParamError(f"Не задан параметр :{name}")
        out.append(sql[last:start])
        out.append("?")
        params.append(values[name])
        last = end
    out.append(sql[last:])
    return "".join(out), params


def _parse_date(text: str) -> dt.date:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError


def coerce_param(p: Param, raw: str | None) -> Any:
    text = (raw if raw is not None else p.default or "").strip()
    if text == "":
        if p.required:
            raise ParamError(f"Параметр «{p.label or p.name}» обязателен.")
        return None
    try:
        if p.type == "int":
            return int(text)
        if p.type == "decimal":
            return Decimal(text.replace(",", "."))
        if p.type == "date":
            return _parse_date(text)
        if p.type == "bool":
            low = text.lower()
            if low in ("1", "true", "yes", "да", "y"):
                return True
            if low in ("0", "false", "no", "нет", "n"):
                return False
            raise ValueError
    except (ValueError, InvalidOperation):
        hint = {"int": "целое число", "decimal": "число", "date": "дата ГГГГ-ММ-ДД или ДД.ММ.ГГГГ",
                "bool": "да/нет"}.get(p.type, "значение")
        raise ParamError(f"Параметр «{p.label or p.name}»: ожидается {hint}.") from None
    return text


def coerce_params(params: list[Param], raw: dict[str, str]) -> dict[str, Any]:
    return {p.name: coerce_param(p, raw.get(p.name)) for p in params}


def render_text(text: str, variables: dict[str, str]) -> tuple[str, list[str]]:
    """Подставляет {{имя}}; возвращает (текст, неизвестные переменные). Неизвестные остаются как есть."""
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        key = m.group(1).strip()
        if key in variables:
            return variables[key]
        if key not in missing:
            missing.append(key)
        return m.group(0)

    return VAR_RE.sub(repl, text or ""), missing
