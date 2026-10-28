"""Генерация скриптов синхронизации (структура и данные). Скрипт ТОЛЬКО формируется — выполнение отдельно,
после просмотра и явного подтверждения. Удаления по умолчанию закомментированы."""
from __future__ import annotations

import datetime as dt
import math
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from database.sql_lexer import lex
from database.sqltext import qualified_name, quote_ident

from .data_diff import DataDiff
from .schema import Column, ForeignKey, Index, Module, Table
from .schema_diff import Difference, SchemaDiff

_SIMPLE_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\((max|\d+(,\s*\d+)?)\))?$", re.I)


@dataclass
class SyncScript:
    text: str
    statements: int = 0
    warnings: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)          # что нужно сделать вручную


# ------------------------------------------------------------ литералы T-SQL
def sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else "NULL"
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, dt.datetime):
        frac = "" if not v.microsecond else (f".{v.microsecond // 1000:03d}" if v.microsecond % 1000 == 0 else f".{v.microsecond:06d}")
        return f"'{v:%Y-%m-%dT%H:%M:%S}{frac}'"
    if isinstance(v, dt.date):
        return f"'{v:%Y-%m-%d}'"
    if isinstance(v, dt.time):
        return f"'{v.isoformat()}'"
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x" + b.hex().upper() if b else "0x"
    if isinstance(v, uuid.UUID):
        return f"'{v}'"
    return "N'" + str(v).replace("'", "''") + "'"


def _type(t: str) -> str:
    return t if _SIMPLE_TYPE.match(t) else quote_ident(t)


def column_sql(c: Column) -> str:
    if c.computed:
        return f"{quote_ident(c.name)} AS {c.computed}"
    parts = [quote_ident(c.name), _type(c.type)]
    if c.collation:
        parts.append(f"COLLATE {c.collation}")
    if c.identity:
        parts.append(f"IDENTITY({c.seed if c.seed is not None else 1},{c.increment if c.increment is not None else 1})")
    parts.append("NULL" if c.nullable else "NOT NULL")
    if c.default:
        parts.append(f"DEFAULT {c.default}")
    return " ".join(parts)


def _col_list(cols: list[str]) -> str:
    out = []
    for c in cols:
        name, desc = (c[:-5], True) if c.upper().endswith(" DESC") else (c, False)
        out.append(quote_ident(name) + (" DESC" if desc else ""))
    return ", ".join(out)


def _tbl(full_schema: str, name: str) -> str:
    return qualified_name(full_schema, name)


def create_table_sql(t: Table) -> str:
    lines = [f"    {column_sql(c)}" for c in sorted(t.columns, key=lambda c: c.ordinal)]
    if t.pk:
        kind = "CLUSTERED" if "CLUSTERED" == t.pk.type_desc.upper() else "NONCLUSTERED"
        lines.append(f"    CONSTRAINT {quote_ident(t.pk.name)} PRIMARY KEY {kind} ({_col_list(t.pk.columns)})")
    return f"CREATE TABLE {_tbl(t.schema, t.name)} (\n" + ",\n".join(lines) + "\n);"


def create_index_sql(table: str, i: Index) -> str | None:
    tbl = table
    if i.unique_constraint:
        return f"ALTER TABLE {tbl} ADD CONSTRAINT {quote_ident(i.name)} UNIQUE ({_col_list(i.columns)});"
    kind = i.type_desc.upper()
    if kind not in ("CLUSTERED", "NONCLUSTERED"):
        return None                                    # columnstore, spatial, XML — вручную
    sql = f"CREATE {'UNIQUE ' if i.unique else ''}{kind} INDEX {quote_ident(i.name)} ON {tbl} ({_col_list(i.columns)})"
    if i.included:
        sql += f" INCLUDE ({_col_list(i.included)})"
    if i.filter:
        sql += f" WHERE {i.filter}"
    return sql + ";"


def drop_index_sql(table: str, i: Index) -> str:
    if i.unique_constraint:
        return f"ALTER TABLE {table} DROP CONSTRAINT {quote_ident(i.name)};"
    return f"DROP INDEX {quote_ident(i.name)} ON {table};"


def fk_sql(table: str, f: ForeignKey) -> str:
    act = lambda a: a.replace("_", " ")
    sql = (f"ALTER TABLE {table} WITH CHECK ADD CONSTRAINT {quote_ident(f.name)} FOREIGN KEY ({_col_list(f.columns)}) "
           f"REFERENCES {_tbl(f.ref_schema, f.ref_table)} ({_col_list(f.ref_columns)})")
    if f.on_delete and f.on_delete != "NO_ACTION":
        sql += f" ON DELETE {act(f.on_delete)}"
    if f.on_update and f.on_update != "NO_ACTION":
        sql += f" ON UPDATE {act(f.on_update)}"
    return sql + ";"


def create_or_alter(definition: str) -> str | None:
    """Заменяет первое CREATE (вне комментариев и строк) на CREATE OR ALTER."""
    for i, t in enumerate(tok for tok in lex(definition).tokens if tok.kind != "comment"):
        if t.kind == "word" and t.upper == "CREATE":
            toks = [x for x in lex(definition).tokens if x.kind != "comment"]
            nxt = toks[i + 1].upper if i + 1 < len(toks) else ""
            if nxt == "OR":
                return definition
            return definition[:t.start] + "CREATE OR ALTER" + definition[t.end:]
        return None
    return None


# ------------------------------------------------------------ структура
def sync_schema_script(diff: SchemaDiff, *, include_drops: bool = False) -> SyncScript:
    out: list[str] = []
    script = SyncScript("")
    n = [0]
    tgt = diff.target

    def stmt(sql: str, note: str = "") -> None:
        if note:
            out.append(f"-- {note}")
        out.append(sql)
        out.append("GO")
        n[0] += 1

    def comment(sql: str, note: str) -> None:
        out.append(f"-- {note}")
        out.extend("-- " + line for line in sql.splitlines())
        out.append("")

    def manual(text: str) -> None:
        script.manual.append(text)
        out.append(f"-- ВРУЧНУЮ: {text}")
        out.append("")

    out += [f"-- Скрипт синхронизации структуры: {diff.source.database} → {tgt.database}",
            f"-- Сформирован DB Workspace Pro {dt.datetime.now():%Y-%m-%d %H:%M}.",
            "-- Проверьте скрипт перед выполнением! Рекомендуется сначала сделать резервную копию целевой базы.",
            "-- CREATE OR ALTER требует SQL Server 2016 SP1 или новее.", ""]
    items = diff.items
    by = lambda cat, st: [d for d in items if d.category == cat and d.status == st]

    for d in by("table", "only_source"):
        stmt(create_table_sql(d.source), f"Новая таблица {d.object}")
    for d in by("column", "only_source"):
        c: Column = d.source
        schema, name = d.table.split(".", 1)
        tbl = _tbl(schema, name)
        if not c.nullable and not c.default and not c.computed:
            tmp = Column(**{**c.__dict__, "nullable": True})
            stmt(f"ALTER TABLE {tbl} ADD {column_sql(tmp)};",
                 f"Новый столбец {d.object}: добавлен как NULL — в исходной базе NOT NULL без значения по умолчанию")
            script.warnings.append(f"{d.object}: добавлен как допускающий NULL (иначе добавление не удастся, если в "
                                   "таблице есть строки). Заполните значения и измените на NOT NULL вручную.")
        else:
            stmt(f"ALTER TABLE {tbl} ADD {column_sql(c)};", f"Новый столбец {d.object}")
    for d in by("column", "changed"):
        c, o = d.source, d.target
        schema, name = d.table.split(".", 1)
        tbl = _tbl(schema, name)
        if c.computed or o.computed or c.identity != o.identity:
            manual(f"{d.object}: изменение вычисляемого столбца или IDENTITY нельзя выполнить ALTER COLUMN — "
                   "пересоздайте столбец вручную.")
            continue
        type_or_null = any(x.startswith(("тип", "NULL", "сортировка")) for x in d.details)
        if type_or_null:
            col = f"{quote_ident(c.name)} {_type(c.type)}" + (f" COLLATE {c.collation}" if c.collation else "") + \
                  (" NULL" if c.nullable else " NOT NULL")
            stmt(f"ALTER TABLE {tbl} ALTER COLUMN {col};",
                 f"ВНИМАНИЕ: изменение {d.object} ({'; '.join(d.details)}) может завершиться ошибкой или потерей данных; "
                 "зависимые индексы и ограничения могут мешать изменению")
            script.warnings.append(f"{d.object}: изменение типа/NULL — проверьте данные и зависимые индексы.")
        if any(x.startswith("значение по умолчанию") for x in d.details):
            if o.default_name:
                stmt(f"ALTER TABLE {tbl} DROP CONSTRAINT {quote_ident(o.default_name)};",
                     f"Значение по умолчанию {d.object}: удалить старое")
            if c.default:
                stmt(f"ALTER TABLE {tbl} ADD DEFAULT {c.default} FOR {quote_ident(c.name)};",
                     f"Значение по умолчанию {d.object}: новое")
    for d in by("pk", "only_source"):
        schema, name = d.table.split(".", 1)
        pk: Index = d.source
        stmt(f"ALTER TABLE {_tbl(schema, name)} ADD CONSTRAINT {quote_ident(pk.name)} PRIMARY KEY "
             f"{'CLUSTERED' if pk.type_desc.upper() == 'CLUSTERED' else 'NONCLUSTERED'} ({_col_list(pk.columns)});",
             f"Первичный ключ {d.object}")
    for d in by("pk", "changed"):
        manual(f"{d.object}: первичный ключ различается ({'; '.join(d.details)}) — изменение затрагивает внешние ключи "
               "и кластерный индекс, выполните вручную.")
    for d in by("index", "only_source") + by("index", "changed"):
        schema, name = d.table.split(".", 1)
        tbl = _tbl(schema, name)
        sql = create_index_sql(tbl, d.source)
        if sql is None:
            manual(f"{d.object}: индекс вида {d.source.type_desc} создайте вручную.")
            continue
        if d.status == "changed":
            stmt(drop_index_sql(tbl, d.target), f"Изменённый индекс {d.object}: пересоздание (удаление старого)")
        stmt(sql, f"Индекс {d.object}")
    for d in by("fk", "changed"):
        schema, name = d.table.split(".", 1)
        tbl = _tbl(schema, name)
        stmt(f"ALTER TABLE {tbl} DROP CONSTRAINT {quote_ident(d.target.name)};", f"Изменённый внешний ключ {d.object}")
        stmt(fk_sql(tbl, d.source))
    for d in by("fk", "only_source"):
        schema, name = d.table.split(".", 1)
        stmt(fk_sql(_tbl(schema, name), d.source), f"Внешний ключ {d.object} (WITH CHECK: проверит существующие данные)")
    for cat in ("view", "function", "procedure", "trigger"):
        for d in by(cat, "only_source") + by(cat, "changed"):
            m: Module = d.source
            if m.definition is None:
                manual(f"{d.object}: определение недоступно (нет права VIEW DEFINITION) — перенесите вручную.")
                continue
            sql = create_or_alter(m.definition)
            if sql is None:
                manual(f"{d.object}: не удалось распознать заголовок CREATE в определении.")
                continue
            stmt(sql.strip(), f"{'Новый' if d.status == 'only_source' else 'Изменённый'} объект: {d.object}")
    for d in [x for x in items if x.status == "unknown"]:
        manual(f"{d.object}: {'; '.join(d.details)}")

    drops = [d for d in items if d.status == "only_target"]
    if drops:
        out.append("-- ===== Объекты, которых нет в исходной базе =====")
        out.append("-- Удаление необратимо. " + ("Удаления включены по вашему выбору." if include_drops else
                                                 "Удаления закомментированы: раскомментируйте только то, что действительно нужно."))
        order = ["fk", "index", "pk", "column", "trigger", "procedure", "function", "view", "table"]
        for cat in order:
            for d in [x for x in drops if x.category == cat]:
                sql = _drop_sql(d)
                if sql is None:
                    continue
                if include_drops:
                    stmt(sql, f"УДАЛЕНИЕ: {_category_ru(d)} {d.object}")
                else:
                    comment(sql, f"{_category_ru(d)} {d.object} есть только в целевой базе")
    if n[0] == 0:
        out.append("-- Изменений, которые можно выполнить автоматически, нет.")
    script.text = "\n".join(out).rstrip() + "\n"
    script.statements = n[0]
    if any(d.status == "only_target" for d in items) and not include_drops:
        script.warnings.append("Объекты, существующие только в целевой базе, не удаляются (команды закомментированы).")
    return script


def _category_ru(d: Difference) -> str:
    from .schema_diff import CATEGORY_RU
    return CATEGORY_RU.get(d.category, d.category)


def _drop_sql(d: Difference) -> str | None:
    t = d.target
    if d.category == "table":
        return f"DROP TABLE {_tbl(t.schema, t.name)};"
    schema, name = (d.table.split(".", 1) + [""])[:2] if d.table else ("", "")
    tbl = _tbl(schema, name) if d.table and "." in d.table else ""
    if d.category == "column":
        return f"ALTER TABLE {tbl} DROP COLUMN {quote_ident(t.name)};"
    if d.category in ("fk", "pk"):
        return f"ALTER TABLE {tbl} DROP CONSTRAINT {quote_ident(t.name)};"
    if d.category == "index":
        return drop_index_sql(tbl, t)
    if d.category in ("view", "procedure", "function", "trigger"):
        word = {"view": "VIEW", "procedure": "PROCEDURE", "function": "FUNCTION", "trigger": "TRIGGER"}[d.category]
        if d.category == "trigger" and not t.schema:
            return f"DROP TRIGGER {quote_ident(t.name)} ON DATABASE;"
        return f"DROP {word} {_tbl(t.schema, t.name) if t.schema else quote_ident(t.name)};"
    return None


# ------------------------------------------------------------ данные
def sync_data_script(schema: str, table: str, columns: list[str], key_columns: list[str], diff: DataDiff, *,
                     include_deletes: bool = False, identity_columns: list[str] | None = None,
                     batch: int = 500) -> SyncScript:
    tbl = _tbl(schema, table)
    out = [f"-- Синхронизация данных таблицы {schema}.{table} (ключ: {', '.join(key_columns)})",
           f"-- Сформирован DB Workspace Pro {dt.datetime.now():%Y-%m-%d %H:%M}. Проверьте перед выполнением.", ""]
    s = SyncScript("")
    n = 0
    idx = {c: i for i, c in enumerate(columns)}
    ident = [c for c in (identity_columns or []) if c in idx]

    def go():
        if n and n % batch == 0:
            out.append("GO")

    if diff.added_total > len(diff.added) or diff.removed_total > len(diff.removed) or diff.changed_total > len(diff.changed):
        s.warnings.append("Сравнение вернуло не все различия (действует ограничение числа элементов) — скрипт неполный. "
                          "Увеличьте лимит и сформируйте скрипт заново.")
        out.append("-- ВНИМАНИЕ: скрипт неполный — см. предупреждение в программе.")
    if diff.added:
        if ident:
            out.append(f"SET IDENTITY_INSERT {tbl} ON;")
        cols = ", ".join(quote_ident(c) for c in columns)
        for row in diff.added:
            out.append(f"INSERT INTO {tbl} ({cols}) VALUES ({', '.join(sql_literal(v) for v in row)});")
            n += 1
            go()
        if ident:
            out.append(f"SET IDENTITY_INSERT {tbl} OFF;")
        out.append("GO")
    for ch in diff.changed:
        sets = ", ".join(f"{quote_ident(c)} = {sql_literal(new)}" for c, (_old, new) in ch.changes.items())
        where = " AND ".join(f"{quote_ident(k)} = {sql_literal(v)}" if v is not None else f"{quote_ident(k)} IS NULL"
                             for k, v in zip(key_columns, ch.key))
        out.append(f"UPDATE {tbl} SET {sets} WHERE {where};")
        n += 1
        go()
    if diff.changed:
        out.append("GO")
    if diff.removed:
        out.append("-- Строки, которых нет в исходной таблице. Удаление необратимо." +
                   ("" if include_deletes else " Команды закомментированы."))
        kidx = [idx[k] for k in key_columns]
        for row in diff.removed:
            where = " AND ".join(f"{quote_ident(k)} = {sql_literal(row[i])}" for k, i in zip(key_columns, kidx))
            line = f"DELETE FROM {tbl} WHERE {where};"
            if include_deletes:
                out.append(line)
                n += 1
                go()
            else:
                out.append("-- " + line)
        if include_deletes:
            out.append("GO")
    if n == 0:
        out.append("-- Изменений для выполнения нет.")
    s.text = "\n".join(out).rstrip() + "\n"
    s.statements = n
    return s
