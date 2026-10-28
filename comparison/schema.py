"""Модель структуры базы данных и её чтение из системных каталогов (оптовыми запросами).

Объекты, на которые у учётной записи нет прав просмотра метаданных, SQL Server не показывает; определения
модулей без права VIEW DEFINITION (или зашифрованные) недоступны — это отмечается, а не скрывается.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field

from database.connection import SqlConnection
from database.errors import DbWorkspaceError
from database.sqltext import format_type, quote_ident


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    identity: bool = False
    seed: int | None = None
    increment: int | None = None
    computed: str = ""                # выражение вычисляемого столбца
    default: str = ""
    collation: str = ""
    ordinal: int = 0
    description: str = ""
    default_name: str = ""            # имя ограничения DEFAULT (нужно для скрипта синхронизации)


@dataclass
class Index:
    name: str
    type_desc: str
    unique: bool
    primary: bool
    unique_constraint: bool
    columns: list[str]
    included: list[str] = field(default_factory=list)
    filter: str = ""


@dataclass
class ForeignKey:
    name: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]
    on_delete: str = "NO_ACTION"
    on_update: str = "NO_ACTION"
    disabled: bool = False


@dataclass
class Param:
    name: str
    type: str
    output: bool = False
    ordinal: int = 0                  # 0 — возвращаемое значение функции


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    fks: list[ForeignKey] = field(default_factory=list)
    rows: int | None = None
    description: str = ""

    @property
    def full(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def pk(self) -> Index | None:
        return next((i for i in self.indexes if i.primary), None)


@dataclass
class Module:
    schema: str
    name: str
    kind: str                         # view | procedure | function | trigger
    type_desc: str = ""
    definition: str | None = None
    parent: str = ""                  # для триггеров — таблица (или «база данных»)
    params: list[Param] = field(default_factory=list)
    description: str = ""
    columns: list[Column] = field(default_factory=list)      # для представлений

    @property
    def full(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class SchemaModel:
    server: str = ""
    database: str = ""
    read_at: str = ""
    tables: dict[str, Table] = field(default_factory=dict)
    modules: dict[str, Module] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @staticmethod
    def key(schema: str, name: str, kind: str = "") -> str:
        return (f"{kind}:" if kind else "") + f"{schema}.{name}".casefold()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SchemaModel":
        m = cls(d.get("server", ""), d.get("database", ""), d.get("read_at", ""), warnings=list(d.get("warnings", [])))
        for k, t in (d.get("tables") or {}).items():
            m.tables[k] = Table(t["schema"], t["name"], [Column(**c) for c in t["columns"]],
                                [Index(**i) for i in t["indexes"]], [ForeignKey(**f) for f in t["fks"]], t.get("rows"),
                                t.get("description", ""))
        for k, x in (d.get("modules") or {}).items():
            m.modules[k] = Module(x["schema"], x["name"], x["kind"], x.get("type_desc", ""), x.get("definition"),
                                  x.get("parent", ""), [Param(**p) for p in x.get("params", [])], x.get("description", ""),
                                  [Column(**c) for c in x.get("columns", [])])
        return m

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def loads(cls, text: str) -> "SchemaModel":
        return cls.from_dict(json.loads(text))

    def only(self, table_names: list[str]) -> "SchemaModel":
        """Копия модели только с указанными таблицами (schema.name) — для сравнения отдельных таблиц."""
        wanted = {n.casefold() if "." in n else f"dbo.{n}".casefold() for n in table_names}
        m = SchemaModel(self.server, self.database, self.read_at, warnings=list(self.warnings))
        m.tables = {k: t for k, t in self.tables.items() if t.full.casefold() in wanted}
        return m


KIND_BY_TYPE = {"V": "view", "P": "procedure", "FN": "function", "IF": "function", "TF": "function", "TR": "trigger"}

_TABLES = ("SELECT t.object_id, s.name, t.name FROM {db}.sys.tables t JOIN {db}.sys.schemas s ON s.schema_id = t.schema_id "
           "WHERE t.is_ms_shipped = 0")
_COLUMNS = (
    "SELECT c.object_id, c.column_id, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity, "
    "c.is_computed, dc.definition, c.collation_name, cc.definition, CAST(ic.seed_value AS bigint), "
    "CAST(ic.increment_value AS bigint), dc.name "
    "FROM {db}.sys.columns c JOIN {db}.sys.types ty ON ty.user_type_id = c.user_type_id "
    "JOIN {db}.sys.objects o ON o.object_id = c.object_id "
    "LEFT JOIN {db}.sys.default_constraints dc ON dc.object_id = c.default_object_id "
    "LEFT JOIN {db}.sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id "
    "LEFT JOIN {db}.sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id "
    "WHERE o.is_ms_shipped = 0 AND o.type IN ('U', 'V') ORDER BY c.object_id, c.column_id")
_INDEXES = (
    "SELECT i.object_id, i.index_id, i.name, i.type_desc, i.is_unique, i.is_primary_key, i.is_unique_constraint, "
    "i.filter_definition, ic.is_included_column, ic.is_descending_key, c.name "
    "FROM {db}.sys.indexes i JOIN {db}.sys.tables t ON t.object_id = i.object_id "
    "JOIN {db}.sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "JOIN {db}.sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
    "WHERE t.is_ms_shipped = 0 AND i.type > 0 AND i.is_hypothetical = 0 "
    "ORDER BY i.object_id, i.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id")
_FKS = (
    "SELECT fk.parent_object_id, fk.object_id, fk.name, fk.delete_referential_action_desc, "
    "fk.update_referential_action_desc, fk.is_disabled, pc.name, rs.name, rt.name, rc.name "
    "FROM {db}.sys.foreign_keys fk "
    "JOIN {db}.sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
    "JOIN {db}.sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id "
    "JOIN {db}.sys.tables rt ON rt.object_id = fkc.referenced_object_id "
    "JOIN {db}.sys.schemas rs ON rs.schema_id = rt.schema_id "
    "JOIN {db}.sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id "
    "ORDER BY fk.parent_object_id, fk.name, fkc.constraint_column_id")
_MODULES = (
    "SELECT o.object_id, ISNULL(s.name, ''), o.name, o.type, o.type_desc, m.definition, ISNULL(ps.name, ''), "
    "ISNULL(po.name, '') FROM {db}.sys.objects o LEFT JOIN {db}.sys.schemas s ON s.schema_id = o.schema_id "
    "LEFT JOIN {db}.sys.sql_modules m ON m.object_id = o.object_id "
    "LEFT JOIN {db}.sys.objects po ON po.object_id = o.parent_object_id "
    "LEFT JOIN {db}.sys.schemas ps ON ps.schema_id = po.schema_id "
    "WHERE o.is_ms_shipped = 0 AND o.type IN ('V', 'P', 'FN', 'IF', 'TF', 'TR')")
_DDL_TRIGGERS = ("SELECT tr.object_id, tr.name, m.definition FROM {db}.sys.triggers tr "
                 "LEFT JOIN {db}.sys.sql_modules m ON m.object_id = tr.object_id WHERE tr.parent_class = 0 "
                 "AND tr.is_ms_shipped = 0")
_PARAMS = (
    "SELECT p.object_id, p.parameter_id, p.name, ty.name, p.max_length, p.precision, p.scale, p.is_output "
    "FROM {db}.sys.parameters p JOIN {db}.sys.types ty ON ty.user_type_id = p.user_type_id "
    "JOIN {db}.sys.objects o ON o.object_id = p.object_id WHERE o.is_ms_shipped = 0 "
    "ORDER BY p.object_id, p.parameter_id")
_DESCRIPTIONS = ("SELECT ep.major_id, ep.minor_id, CAST(ep.value AS nvarchar(4000)) FROM {db}.sys.extended_properties ep "
                 "WHERE ep.class = 1 AND ep.name = N'MS_Description'")
_ROWS = ("SELECT p.object_id, SUM(p.rows) FROM {db}.sys.partitions p JOIN {db}.sys.tables t ON t.object_id = p.object_id "
         "WHERE p.index_id IN (0, 1) GROUP BY p.object_id")


class SchemaReadError(DbWorkspaceError):
    pass


def read_schema(conn: SqlConnection, database: str, *, server: str = "", cancel_token=None,
                progress=lambda stage, frac: None) -> SchemaModel:
    db = quote_ident(database)
    model = SchemaModel(server, database, dt.datetime.now().isoformat(timespec="seconds"))

    def rows(sql, label, required=False, frac=None):
        if cancel_token is not None and cancel_token.is_cancelled:
            from database.errors import QueryCancelledError
            raise QueryCancelledError("Операция отменена.")
        progress(f"Чтение структуры: {label}", frac)
        try:
            return conn.fetch_rows(sql.format(db=db), safe_to_retry=True, cancel_token=cancel_token).rows
        except DbWorkspaceError as exc:
            if required:
                raise SchemaReadError(f"Не удалось прочитать {label}: {exc}") from exc
            model.warnings.append(f"{label[:1].upper() + label[1:]} недоступны: {exc}")
            return []

    by_id: dict[int, Table] = {}
    for oid, s, n in rows(_TABLES, "таблицы", True, 0.05):
        t = Table(str(s), str(n))
        by_id[int(oid)] = t
        model.tables[SchemaModel.key(t.schema, t.name)] = t
    mods: dict[int, Module] = {}
    for oid, s, n, typ, typ_desc, definition, ps, pn in rows(_MODULES, "представления, процедуры, функции, триггеры",
                                                            False, 0.15):
        kind = KIND_BY_TYPE.get(str(typ).strip(), "")
        if not kind:
            continue
        m = Module(str(s), str(n), kind, str(typ_desc or ""), definition,
                   f"{ps}.{pn}" if kind == "trigger" and pn else "")
        mods[int(oid)] = m
        model.modules[SchemaModel.key(m.schema, m.name, kind)] = m
    for oid, n, definition in rows(_DDL_TRIGGERS, "триггеры уровня базы данных", False, 0.2):
        m = Module("", str(n), "trigger", "DATABASE_DDL_TRIGGER", definition, "(база данных)")
        mods[int(oid)] = m
        model.modules[SchemaModel.key("", m.name, "trigger")] = m
    for r in rows(_COLUMNS, "столбцы", True, 0.35):
        oid = int(r[0])
        target = by_id.get(oid) or mods.get(oid)
        if target is None:
            continue
        col = Column(str(r[2]), format_type(str(r[3]), int(r[4]), int(r[5]), int(r[6])), bool(r[7]), bool(r[8]),
                     int(r[13]) if r[13] is not None else None, int(r[14]) if r[14] is not None else None,
                     str(r[12] or "") if r[9] else "", str(r[10] or ""), str(r[11] or ""), int(r[1]), "",
                     str(r[15] or "") if len(r) > 15 else "")
        target.columns.append(col)
    idx: dict[tuple[int, int], Index] = {}
    for oid, iid, name, typ, uq, pk, uc, flt, inc, desc, col in rows(_INDEXES, "индексы", False, 0.5):
        t = by_id.get(int(oid))
        if t is None:
            continue
        key = (int(oid), int(iid))
        if key not in idx:
            idx[key] = Index(str(name), str(typ), bool(uq), bool(pk), bool(uc), [], [], str(flt or ""))
            t.indexes.append(idx[key])
        (idx[key].included if inc else idx[key].columns).append(str(col) + (" DESC" if desc and not inc else ""))
    fks: dict[int, ForeignKey] = {}
    for poid, fid, name, od, ou, dis, pc, rs, rt, rc in rows(_FKS, "внешние ключи", False, 0.6):
        t = by_id.get(int(poid))
        if t is None:
            continue
        if int(fid) not in fks:
            fks[int(fid)] = ForeignKey(str(name), [], str(rs), str(rt), [], str(od), str(ou), bool(dis))
            t.fks.append(fks[int(fid)])
        fks[int(fid)].columns.append(str(pc))
        fks[int(fid)].ref_columns.append(str(rc))
    for oid, pid, name, typ, ml, pr, sc, out in rows(_PARAMS, "параметры процедур и функций", False, 0.7):
        m = mods.get(int(oid))
        if m is not None:
            m.params.append(Param(str(name or ""), format_type(str(typ), int(ml), int(pr), int(sc)), bool(out), int(pid)))
    for major, minor, value in rows(_DESCRIPTIONS, "описания объектов (MS_Description)", False, 0.8):
        target = by_id.get(int(major)) or mods.get(int(major))
        if target is None or value is None:
            continue
        if int(minor) == 0:
            target.description = str(value)
        else:
            c = next((c for c in target.columns if c.ordinal == int(minor)), None)
            if c is not None:
                c.description = str(value)
    for oid, cnt in rows(_ROWS, "число строк", False, 0.9):
        t = by_id.get(int(oid))
        if t is not None and cnt is not None:
            t.rows = int(cnt)
    hidden = sum(1 for m in model.modules.values() if m.definition is None)
    if hidden:
        model.warnings.append(f"Определения недоступны для объектов: {hidden} (нет права VIEW DEFINITION или объект "
                              "зашифрован).")
    progress("Чтение структуры завершено", 1.0)
    return model
