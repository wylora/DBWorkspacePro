"""Получение данных для отчёта: только чтение, с лимитом строк, отменой и защитой от изменений."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from database.connection import SqlConnection
from database.editor_session import ExecutionListener
from database.errors import DbWorkspaceError, QueryCancelledError, StopExecution
from database.sql_guard import analyze
from database.sqltext import quote_ident
from utils.export import unique_names
from utils.logging_setup import redact
from .model import DataSource
from .query_builder import build_query

log = logging.getLogger("dbworkspace.reports")


class SourceError(DbWorkspaceError):
    pass


@dataclass
class Dataset:
    source_id: str
    name: str
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    elapsed: float = 0.0
    sql: str = ""
    warnings: list[str] = field(default_factory=list)


class _FirstRowset(ExecutionListener):
    """Берёт только первый набор строк (остальные наборы пропускаются) и останавливает выполнение."""

    def __init__(self, cap: int | None):
        self.cap = cap
        self.columns: list[str] | None = None
        self.rows: list[tuple] = []
        self.truncated = False
        self._index: int | None = None

    def on_result(self, index, columns):
        if self.columns is None:
            self.columns, self._index = list(columns), index
            return self.cap
        return 0

    def on_rows(self, index, rows):
        if index == self._index:
            self.rows.extend(rows)

    def on_result_done(self, index, count, truncated):
        if index == self._index:
            self.truncated = truncated
            raise StopExecution()


def check_read_only(sql: str) -> None:
    a = analyze(sql)
    if a.errors or len(a.batches) != 1:
        raise SourceError("В источнике допускается ровно один пакет без GO.")
    if not a.is_read_only:
        raise SourceError("Запрос изменяет данные или схему — для отчётов разрешено только чтение: "
                          + "; ".join(a.reasons[:2]))


def fetch_dataset(conn: SqlConnection, source: DataSource, params: dict[str, Any], *, max_rows: int,
                  batch_size: int = 1000, cancel_token=None, current_db: list[str | None] | None = None) -> Dataset:
    """Читает первый набор результатов источника (не более max_rows строк)."""
    sql, values = build_query(source, params, max_rows)
    if source.kind == "procedure":
        if not source.proc_trusted:
            raise SourceError(f"Источник «{source.name or source.id}»: процедура не подтверждена как «только чтение».")
    else:
        try:
            check_read_only(sql)
        except SourceError as exc:
            raise SourceError(f"Источник «{source.name or source.id}»: {exc}") from exc
    started = time.perf_counter()
    cap = source.row_limit if 0 < source.row_limit < max_rows else max_rows
    listener = _FirstRowset(cap)
    try:
        if source.kind != "table" and source.database and (current_db is None or current_db[0] != source.database):
            conn.execute_multi("USE " + quote_ident(source.database), listener=ExecutionListener())
            if current_db is not None:
                current_db[0] = source.database
        try:
            conn.execute_multi(sql, values, listener=listener, batch_size=batch_size, cancel_token=cancel_token)
        except StopExecution:
            pass
    except QueryCancelledError:
        raise
    except DbWorkspaceError as exc:
        raise SourceError(f"Источник «{source.name or source.id}»: {exc}") from exc
    if listener.columns is None:
        raise SourceError(f"Источник «{source.name or source.id}» не вернул набор строк.")
    ds = Dataset(source.id, source.name or source.id, unique_names(listener.columns), listener.rows,
                 listener.truncated,
                 time.perf_counter() - started, redact(sql))
    if ds.truncated:
        ds.warnings.append(f"Источник «{ds.name}»: получены первые {len(ds.rows)} строк, в источнике их больше.")
    return ds
