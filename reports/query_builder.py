"""Построение безопасных SELECT-запросов для источников «таблица/представление» и «процедура»."""
from __future__ import annotations

from typing import Any

from database.sqltext import qualified_name, quote_ident
from .model import AGG_FUNCS, FILTER_OPS, DataSource
from .variables import ParamError, convert_named_params

_FUNCS = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT"}


class QueryBuildError(ValueError):
    pass


def _value(raw: str, params: dict[str, Any]) -> Any:
    if raw.startswith(":"):
        name = raw[1:]
        if name not in params:
            raise ParamError(f"Не задан параметр :{name}")
        return params[name]
    return raw


def build_table_query(src: DataSource, params: dict[str, Any], max_rows: int = 0) -> tuple[str, list[Any]]:
    if not src.table.strip():
        raise QueryBuildError("Не указана таблица.")
    values: list[Any] = []
    grouping = bool(src.group_by or src.aggregates)
    if grouping:
        cols = [quote_ident(c) for c in src.group_by]
        for i, a in enumerate(src.aggregates):
            if a.func not in AGG_FUNCS:
                raise QueryBuildError(f"Неизвестная агрегатная функция: {a.func}")
            target = "*" if a.column in ("*", "") else quote_ident(a.column)
            if a.func == "count_distinct":
                expr = f"COUNT(DISTINCT {target})"
            else:
                expr = f"{_FUNCS[a.func]}({target})"
            alias = a.alias or f"{a.func}_{a.column if a.column not in ('*', '') else 'all'}"
            cols.append(f"{expr} AS {quote_ident(alias)}")
        select = ", ".join(cols)
    else:
        select = ", ".join(quote_ident(c) for c in src.columns) if src.columns else "*"
    limit = src.row_limit or max_rows
    top = f"TOP ({int(limit)}) " if limit and limit > 0 else ""
    sql = f"SELECT {top}{select} FROM {qualified_name(src.database, src.schema, src.table)}"

    where = []
    for f in src.filters:
        if f.op not in FILTER_OPS:
            raise QueryBuildError(f"Неизвестный оператор фильтра: {f.op}")
        col = quote_ident(f.column)
        if f.op == "is_null":
            where.append(f"{col} IS NULL")
        elif f.op == "not_null":
            where.append(f"{col} IS NOT NULL")
        elif f.op == "in":
            raw = _value(f.value, params)
            items = [x.strip() for x in str(raw).split(";") if x.strip()] if raw is not None else []
            if not items:
                raise QueryBuildError(f"Фильтр IN по «{f.column}»: не заданы значения.")
            where.append(f"{col} IN ({', '.join('?' for _ in items)})")
            values.extend(items)
        else:
            op = "LIKE" if f.op == "like" else f.op
            where.append(f"{col} {op} ?")
            values.append(_value(f.value, params))
    if where:
        sql += " WHERE " + " AND ".join(where)
    if grouping and src.group_by:
        sql += " GROUP BY " + ", ".join(quote_ident(c) for c in src.group_by)
    if src.order_by:
        sql += " ORDER BY " + ", ".join(quote_ident(o.column) + (" DESC" if o.desc else "") for o in src.order_by)
    return sql, values


def build_procedure_call(src: DataSource, params: dict[str, Any]) -> tuple[str, list[Any]]:
    parts = [p for p in src.procedure.replace("[", "").replace("]", "").split(".") if p]
    if not parts or len(parts) > 3:
        raise QueryBuildError("Процедура указывается как схема.имя")
    sql = "EXEC " + qualified_name(*parts)
    values: list[Any] = []
    args = []
    for name, raw in src.proc_params.items():
        args.append(f"@{name} = ?")
        values.append(_value(raw, params))
    if args:
        sql += " " + ", ".join(args)
    return sql, values


def build_query(src: DataSource, params: dict[str, Any], max_rows: int = 0) -> tuple[str, list[Any]]:
    if src.kind == "sql":
        return convert_named_params(src.sql, params)
    if src.kind == "table":
        return build_table_query(src, params, max_rows)
    if src.kind == "procedure":
        return build_procedure_call(src, params)
    raise QueryBuildError(f"Неизвестный тип источника: {src.kind}")
