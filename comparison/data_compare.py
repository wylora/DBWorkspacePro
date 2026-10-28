"""Сравнение данных двух таблиц (возможно, на разных серверах) по ключу."""
from __future__ import annotations

from dataclasses import dataclass, field

from database.connection import SqlConnection
from database.errors import DbWorkspaceError
from database.sqltext import qualified_name, quote_ident

from .data_diff import DataDiff, DiffError, diff_tables


class CompareError(DbWorkspaceError):
    pass


@dataclass
class TableRef:
    profile_id: str
    database: str
    schema: str
    table: str

    @property
    def label(self) -> str:
        return f"{self.database}.{self.schema}.{self.table}"


@dataclass
class DataCompareRequest:
    source: TableRef
    target: TableRef
    key_columns: list[str]
    compare_columns: list[str] = field(default_factory=list)
    max_rows: int = 200_000
    key_casefold: bool = True
    max_items: int = 5_000


@dataclass
class DataCompareResult:
    request: DataCompareRequest
    diff: DataDiff
    columns: list[str]
    source_rows: int
    target_rows: int
    identity_columns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def read_table(conn: SqlConnection, ref: TableRef, columns: list[str] | None, max_rows: int, cancel_token=None):
    cols = ", ".join(quote_ident(c) for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM {qualified_name(ref.database, ref.schema, ref.table)}"
    try:
        res = conn.fetch_rows(sql, max_rows=max_rows, safe_to_retry=True, cancel_token=cancel_token)
    except DbWorkspaceError as exc:
        raise CompareError(f"Не удалось прочитать {ref.label}: {exc}") from exc
    if res.truncated:
        raise CompareError(f"Таблица {ref.label} содержит больше {max_rows} строк. Увеличьте лимит в настройках: "
                           "сравнение неполной выборки дало бы ложные «удалённые» и «новые» записи.")
    return res.columns, res.rows


def identity_columns(conn: SqlConnection, ref: TableRef) -> list[str]:
    try:
        res = conn.fetch_rows(f"SELECT c.name FROM {quote_ident(ref.database)}.sys.identity_columns c "
                              f"WHERE c.object_id = OBJECT_ID(?)", (qualified_name(ref.database, ref.schema, ref.table),),
                              safe_to_retry=True)
        return [str(r[0]) for r in res.rows]
    except DbWorkspaceError:
        return []


def compare_data(profiles, req: DataCompareRequest, *, cancel_token=None, progress=lambda s, f: None) -> DataCompareResult:
    if not req.key_columns:
        raise CompareError("Укажите ключевые столбцы для сопоставления строк.")
    cols = list(dict.fromkeys(req.key_columns + req.compare_columns)) if req.compare_columns else None
    progress(f"Чтение {req.source.label}", 0.1)
    with profiles.lease(req.source.profile_id, "compare-src") as c1:
        sc, sr = read_table(c1, req.source, cols, req.max_rows, cancel_token)
    progress(f"Чтение {req.target.label}", 0.5)
    with profiles.lease(req.target.profile_id, "compare-tgt") as c2:
        tc, tr = read_table(c2, req.target, cols, req.max_rows, cancel_token)
        ident = identity_columns(c2, req.target)
    progress("Сравнение", 0.85)
    try:
        # «старое» — целевая таблица, «новое» — исходная: added = есть только в исходной (их нужно добавить в целевую)
        d = diff_tables(tc, tr, sc, sr, req.key_columns, req.compare_columns or None, req.max_items, req.key_casefold)
    except DiffError as exc:
        raise CompareError(str(exc)) from exc
    notes = []
    if req.key_casefold:
        notes.append("Текстовые ключи сравнивались без учёта регистра (как при сортировке SQL Server по умолчанию).")
    return DataCompareResult(req, d, sc, len(sr), len(tr), ident, notes)
