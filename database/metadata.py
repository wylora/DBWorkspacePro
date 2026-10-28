"""Чтение метаданных SQL Server через системные представления sys.*.

Имена баз вставляются только через quote_ident, значения — параметрами (?).
Объекты, на которые у пользователя нет прав просмотра метаданных, SQL Server
из sys.* просто не возвращает — это ограничение самого сервера.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .connection import SqlConnection
from .errors import DbWorkspaceError, MetadataError, QueryCancelledError
from .sqltext import format_type, quote_ident

KINDS = ("table", "view", "procedure", "function", "trigger")

_OBJECT_SQL = {
    "table": ("SELECT s.name, o.name, o.object_id, o.type_desc, o.create_date, '' "
              "FROM {db}.sys.tables o JOIN {db}.sys.schemas s ON s.schema_id = o.schema_id "
              "WHERE o.is_ms_shipped = 0 ORDER BY s.name, o.name"),
    "view": ("SELECT s.name, o.name, o.object_id, o.type_desc, o.create_date, '' "
             "FROM {db}.sys.views o JOIN {db}.sys.schemas s ON s.schema_id = o.schema_id "
             "WHERE o.is_ms_shipped = 0 ORDER BY s.name, o.name"),
    "procedure": ("SELECT s.name, o.name, o.object_id, o.type_desc, o.create_date, '' "
                  "FROM {db}.sys.procedures o JOIN {db}.sys.schemas s ON s.schema_id = o.schema_id "
                  "WHERE o.is_ms_shipped = 0 ORDER BY s.name, o.name"),
    "function": ("SELECT s.name, o.name, o.object_id, o.type_desc, o.create_date, '' "
                 "FROM {db}.sys.objects o JOIN {db}.sys.schemas s ON s.schema_id = o.schema_id "
                 "WHERE o.type IN ('FN','IF','TF','FS','FT') AND o.is_ms_shipped = 0 ORDER BY s.name, o.name"),
    "trigger": ("SELECT ISNULL(s.name, ''), tr.name, tr.object_id, tr.type_desc, tr.create_date, ISNULL(po.name, '') "
                "FROM {db}.sys.triggers tr "
                "LEFT JOIN {db}.sys.objects po ON po.object_id = tr.parent_id "
                "LEFT JOIN {db}.sys.schemas s ON s.schema_id = po.schema_id "
                "WHERE tr.is_ms_shipped = 0 ORDER BY s.name, tr.name"),
}

_COLUMNS_SQL = (
    "SELECT c.column_id, c.name, ty.name, c.max_length, c.precision, c.scale, "
    "c.is_nullable, c.is_identity, c.is_computed, dc.definition "
    "FROM {db}.sys.columns c "
    "JOIN {db}.sys.types ty ON ty.user_type_id = c.user_type_id "
    "LEFT JOIN {db}.sys.default_constraints dc ON dc.object_id = c.default_object_id "
    "WHERE c.object_id = ? ORDER BY c.column_id")

_INDEXES_SQL = (
    "SELECT i.index_id, i.name, i.type_desc, i.is_unique, i.is_primary_key, i.is_unique_constraint, "
    "ic.is_included_column, ic.is_descending_key, c.name "
    "FROM {db}.sys.indexes i "
    "JOIN {db}.sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "JOIN {db}.sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
    "WHERE i.object_id = ? AND i.type > 0 AND i.is_hypothetical = 0 "
    "ORDER BY i.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id")

_FK_SQL = (
    "SELECT fk.object_id, fk.name, fk.delete_referential_action_desc, fk.update_referential_action_desc, "
    "fk.is_disabled, pc.name, rs.name, rt.name, rc.name "
    "FROM {db}.sys.foreign_keys fk "
    "JOIN {db}.sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
    "JOIN {db}.sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id "
    "JOIN {db}.sys.tables rt ON rt.object_id = fkc.referenced_object_id "
    "JOIN {db}.sys.schemas rs ON rs.schema_id = rt.schema_id "
    "JOIN {db}.sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id "
    "WHERE fk.parent_object_id = ? ORDER BY fk.name, fkc.constraint_column_id")

_ALL_COLUMNS_SQL = (
    "SELECT s.name, o.name, c.name, ty.name, c.max_length, c.precision, c.scale "
    "FROM {db}.sys.columns c "
    "JOIN {db}.sys.objects o ON o.object_id = c.object_id "
    "JOIN {db}.sys.schemas s ON s.schema_id = o.schema_id "
    "JOIN {db}.sys.types ty ON ty.user_type_id = c.user_type_id "
    "WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0 ORDER BY s.name, o.name, c.column_id")

_ROWS_SQL = ("SELECT SUM(p.rows) FROM {db}.sys.partitions p "
             "WHERE p.object_id = ? AND p.index_id IN (0, 1)")
_DEFINITION_SQL = "SELECT m.definition FROM {db}.sys.sql_modules m WHERE m.object_id = ?"
_DATABASES_SQL = "SELECT name FROM sys.databases WHERE HAS_DBACCESS(name) = 1 AND state = 0 ORDER BY name"


@dataclass(frozen=True)
class ObjectInfo:
    schema: str
    name: str
    object_id: int
    kind: str
    type_desc: str = ""
    created: datetime | None = None
    parent: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class ObjectList:
    items: list[ObjectInfo]
    truncated: bool = False


@dataclass(frozen=True)
class ColumnInfo:
    ordinal: int
    name: str
    data_type: str
    is_nullable: bool
    is_identity: bool = False
    is_computed: bool = False
    default: str = ""
    is_primary_key: bool = False

    def describe(self) -> str:
        flags = []
        if self.is_primary_key:
            flags.append("PK")
        flags.append("NULL" if self.is_nullable else "NOT NULL")
        if self.is_identity:
            flags.append("IDENTITY")
        if self.is_computed:
            flags.append("вычисляемый")
        return f"{self.data_type}, {', '.join(flags)}"


@dataclass(frozen=True)
class IndexInfo:
    name: str
    type_desc: str
    is_unique: bool
    is_primary_key: bool
    is_unique_constraint: bool
    columns: tuple[str, ...]
    included_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForeignKeyInfo:
    name: str
    columns: tuple[str, ...]
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]
    on_delete: str = ""
    on_update: str = ""
    is_disabled: bool = False


@dataclass
class TableDetails:
    columns: list[ColumnInfo]
    indexes: list[IndexInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    row_estimate: int | None = None
    warnings: list[str] = field(default_factory=list)


class MetadataReader:
    def __init__(self, connection: SqlConnection, *, max_objects: int = 20000, batch_size: int = 1000):
        self._conn = connection
        self._max_objects = max_objects
        self._batch = batch_size

    def _rows(self, sql: str, params: tuple = (), max_rows: int | None = None):
        try:
            return self._conn.fetch_rows(sql, params, max_rows=max_rows, batch_size=self._batch,
                                         safe_to_retry=True)
        except QueryCancelledError:
            raise
        except DbWorkspaceError as exc:
            raise MetadataError(str(exc)) from exc

    def list_databases(self) -> list[str]:
        return [str(r[0]) for r in self._rows(_DATABASES_SQL).rows]

    def list_objects(self, database: str, kind: str) -> ObjectList:
        if kind not in _OBJECT_SQL:
            raise ValueError(f"Неизвестный тип объекта: {kind}")
        sql = _OBJECT_SQL[kind].format(db=quote_ident(database))
        res = self._rows(sql, max_rows=self._max_objects)
        items = [ObjectInfo(str(r[0]), str(r[1]), int(r[2]), kind, str(r[3] or ""), r[4], str(r[5] or ""))
                 for r in res.rows]
        return ObjectList(items, res.truncated)

    def get_table_details(self, database: str, object_id: int) -> TableDetails:
        db = quote_ident(database)
        col_rows = self._rows(_COLUMNS_SQL.format(db=db), (object_id,)).rows
        details = TableDetails(columns=[])
        warnings = details.warnings

        indexes: list[IndexInfo] = []
        try:
            indexes = self._parse_indexes(self._rows(_INDEXES_SQL.format(db=db), (object_id,)).rows)
        except MetadataError as exc:
            warnings.append(f"Индексы недоступны: {exc}")
        pk_cols = {c.split(" ")[0] for ix in indexes if ix.is_primary_key for c in ix.columns}

        details.columns = [
            ColumnInfo(int(r[0]), str(r[1]), format_type(str(r[2]), int(r[3]), int(r[4]), int(r[5])),
                       bool(r[6]), bool(r[7]), bool(r[8]), str(r[9] or ""), str(r[1]) in pk_cols)
            for r in col_rows]
        details.indexes = indexes

        try:
            details.foreign_keys = self._parse_fks(self._rows(_FK_SQL.format(db=db), (object_id,)).rows)
        except MetadataError as exc:
            warnings.append(f"Внешние ключи недоступны: {exc}")
        try:
            rows = self._rows(_ROWS_SQL.format(db=db), (object_id,)).rows
            if rows and rows[0][0] is not None:
                details.row_estimate = int(rows[0][0])
        except MetadataError as exc:
            warnings.append(f"Число строк недоступно: {exc}")
        return details

    def list_all_columns(self, database: str, max_rows: int = 50_000):
        """{(схема, объект): [(столбец, тип)]} для всех таблиц и представлений + признак усечения."""
        res = self._rows(_ALL_COLUMNS_SQL.format(db=quote_ident(database)), max_rows=max_rows)
        out: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for r in res.rows:
            out.setdefault((str(r[0]), str(r[1])), []).append(
                (str(r[2]), format_type(str(r[3]), int(r[4]), int(r[5]), int(r[6]))))
        return out, res.truncated

    def get_definition(self, database: str, object_id: int) -> str | None:
        rows = self._rows(_DEFINITION_SQL.format(db=quote_ident(database)), (object_id,)).rows
        if rows and rows[0][0] is not None:
            return str(rows[0][0])
        return None

    @staticmethod
    def _parse_indexes(rows) -> list[IndexInfo]:
        grouped: dict[int, dict] = {}
        for r in rows:
            g = grouped.setdefault(int(r[0]), {
                "name": str(r[1]), "type": str(r[2]), "unique": bool(r[3]), "pk": bool(r[4]),
                "uc": bool(r[5]), "keys": [], "inc": []})
            col = str(r[8]) + (" DESC" if r[7] and not r[6] else "")
            (g["inc"] if r[6] else g["keys"]).append(col)
        return [IndexInfo(g["name"], g["type"], g["unique"], g["pk"], g["uc"], tuple(g["keys"]), tuple(g["inc"]))
                for _, g in sorted(grouped.items())]

    @staticmethod
    def _parse_fks(rows) -> list[ForeignKeyInfo]:
        grouped: dict[int, dict] = {}
        for r in rows:
            g = grouped.setdefault(int(r[0]), {
                "name": str(r[1]), "del": str(r[2]), "upd": str(r[3]), "dis": bool(r[4]),
                "rs": str(r[6]), "rt": str(r[7]), "cols": [], "rcols": []})
            g["cols"].append(str(r[5]))
            g["rcols"].append(str(r[8]))
        return [ForeignKeyInfo(g["name"], tuple(g["cols"]), g["rs"], g["rt"], tuple(g["rcols"]),
                               g["del"], g["upd"], g["dis"])
                for _, g in sorted(grouped.items(), key=lambda kv: kv[1]["name"])]
