"""Сбор показателей мониторинга. Показатели без данных или без прав не выдумываются — вместо них заметка."""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field

from database.connection import SqlConnection
from database.errors import DbWorkspaceError
from database.sqltext import quote_ident

SERVER_STATE_SQL = "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE')"
ALL_SIZES_SQL = ("SELECT DB_NAME(database_id), SUM(CASE WHEN type = 0 THEN size ELSE 0 END) * 8 / 1024.0, "
                 "SUM(CASE WHEN type = 1 THEN size ELSE 0 END) * 8 / 1024.0 FROM sys.master_files "
                 "GROUP BY database_id ORDER BY DB_NAME(database_id)")
DB_SIZE_SQL = ("SELECT SUM(CASE WHEN type = 0 THEN size ELSE 0 END) * 8 / 1024.0, "
               "SUM(CASE WHEN type = 1 THEN size ELSE 0 END) * 8 / 1024.0 FROM {db}.sys.database_files")
TABLE_COUNT_SQL = "SELECT COUNT(*) FROM {db}.sys.tables WHERE is_ms_shipped = 0"
TABLE_SIZES_SQL = (
    "SELECT TOP (?) s.name, t.name, SUM(CASE WHEN p.index_id IN (0, 1) THEN p.rows ELSE 0 END), "
    "SUM(a.total_pages) * 8 / 1024.0 "
    "FROM {db}.sys.tables t JOIN {db}.sys.schemas s ON s.schema_id = t.schema_id "
    "JOIN {db}.sys.partitions p ON p.object_id = t.object_id "
    "JOIN {db}.sys.allocation_units a ON a.container_id = p.partition_id "
    "WHERE t.is_ms_shipped = 0 GROUP BY s.name, t.name ORDER BY 4 DESC, 3 DESC")
TABLE_ROWS_SQL = (
    "SELECT TOP (?) s.name, t.name, SUM(p.rows) FROM {db}.sys.tables t "
    "JOIN {db}.sys.schemas s ON s.schema_id = t.schema_id "
    "JOIN {db}.sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
    "WHERE t.is_ms_shipped = 0 GROUP BY s.name, t.name ORDER BY 3 DESC")
REQUESTS_SQL = (
    "SELECT TOP (50) r.session_id, r.status, r.command, DB_NAME(r.database_id), r.wait_type, r.wait_time, "
    "r.total_elapsed_time, r.blocking_session_id, s.login_name, s.host_name, s.program_name, "
    "SUBSTRING(t.text, 1, 300) FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id "
    "OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t WHERE r.session_id <> @@SPID AND s.is_user_process = 1 "
    "ORDER BY r.total_elapsed_time DESC")
LOCKS_SQL = (
    "SELECT TOP (50) request_session_id, resource_type, DB_NAME(resource_database_id), request_mode, request_status, "
    "COUNT(*) FROM sys.dm_tran_locks WHERE request_session_id <> @@SPID "
    "GROUP BY request_session_id, resource_type, resource_database_id, request_mode, request_status "
    "ORDER BY CASE request_status WHEN 'WAIT' THEN 0 ELSE 1 END, COUNT(*) DESC")


@dataclass
class Probe:
    ok: bool
    latency_ms: float | None = None
    error: str = ""


@dataclass
class TableSize:
    schema: str
    name: str
    rows: int
    size_mb: float | None


@dataclass
class MonitorSnapshot:
    ts: dt.datetime
    probe: Probe
    database: str = ""
    server_state: bool | None = None
    db_sizes: list[tuple[str, float, float]] | None = None     # (база, данные МБ, журнал МБ)
    table_count: int | None = None
    tables: list[TableSize] | None = None
    requests: list[dict] | None = None
    locks: list[dict] | None = None
    notes: list[str] = field(default_factory=list)


def probe(conn: SqlConnection) -> Probe:
    started = time.perf_counter()
    try:
        conn.fetch_rows("SELECT 1", safe_to_retry=False)
    except DbWorkspaceError as exc:
        return Probe(False, None, str(exc))
    return Probe(True, round((time.perf_counter() - started) * 1000, 1))


def _rows(conn, sql, params=()):
    return conn.fetch_rows(sql, params, safe_to_retry=True).rows


def _first(conn, sql, params=()):
    """Первая строка ответа или None (пустой ответ — это «нет данных», а не ошибка программы)."""
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def collect(conn: SqlConnection, database: str = "", *, top_tables: int = 15, include_activity: bool = True
            ) -> MonitorSnapshot:
    snap = MonitorSnapshot(dt.datetime.now(), probe(conn), database)
    if not snap.probe.ok:
        snap.notes.append("Сервер недоступен — остальные показатели не собирались.")
        return snap
    db = quote_ident(database) if database else ""
    try:
        row = _first(conn, SERVER_STATE_SQL)
        snap.server_state = bool(row[0]) if row else None
    except DbWorkspaceError:
        snap.server_state = None
    try:
        snap.db_sizes = [(str(r[0]), float(r[1] or 0), float(r[2] or 0)) for r in _rows(conn, ALL_SIZES_SQL) if r[0]]
    except DbWorkspaceError:
        if database:
            try:
                r = _first(conn, DB_SIZE_SQL.format(db=db))
                snap.db_sizes = [(database, float(r[0] or 0), float(r[1] or 0))] if r else None
                snap.notes.append("Размеры всех баз недоступны (нужно право VIEW ANY DEFINITION) — показан размер "
                                  "выбранной базы.")
            except DbWorkspaceError:
                snap.notes.append("Размер базы недоступен: недостаточно прав.")
        else:
            snap.notes.append("Размеры баз недоступны: недостаточно прав. Выберите базу данных.")
    if database:
        try:
            r = _first(conn, TABLE_COUNT_SQL.format(db=db))
            snap.table_count = int(r[0]) if r and r[0] is not None else None
        except DbWorkspaceError:
            snap.notes.append("Число таблиц недоступно: нет доступа к базе.")
        try:
            snap.tables = [TableSize(str(r[0]), str(r[1]), int(r[2] or 0), float(r[3] or 0))
                           for r in _rows(conn, TABLE_SIZES_SQL.format(db=db), (top_tables,))]
        except DbWorkspaceError:
            try:
                snap.tables = [TableSize(str(r[0]), str(r[1]), int(r[2] or 0), None)
                               for r in _rows(conn, TABLE_ROWS_SQL.format(db=db), (top_tables,))]
                snap.notes.append("Объём таблиц на диске недоступен (нет прав на sys.allocation_units) — "
                                  "показано только число строк.")
            except DbWorkspaceError:
                snap.notes.append("Сведения о таблицах недоступны.")
    else:
        snap.notes.append("Выберите базу данных, чтобы увидеть таблицы.")
    if include_activity:
        if snap.server_state:
            try:
                snap.requests = [dict(session=r[0], status=r[1], command=r[2], database=r[3], wait_type=r[4],
                                      wait_ms=r[5], elapsed_ms=r[6], blocked_by=r[7], login=r[8], host=r[9],
                                      program=r[10], text=r[11]) for r in _rows(conn, REQUESTS_SQL)]
                snap.locks = [dict(session=r[0], resource=r[1], database=r[2], mode=r[3], status=r[4], count=r[5])
                              for r in _rows(conn, LOCKS_SQL)]
            except DbWorkspaceError as exc:
                snap.notes.append(f"Текущие запросы и блокировки недоступны: {exc}")
        else:
            snap.notes.append("Текущие запросы и блокировки не показываются: нужно право VIEW SERVER STATE "
                              "(без него видны только собственные сеансы, и картина была бы неполной).")
    return snap


def metrics_from(snap: MonitorSnapshot) -> list[tuple[str, str, float | None]]:
    items: list[tuple[str, str, float | None]] = [("available", "", 1.0 if snap.probe.ok else 0.0)]
    if snap.probe.latency_ms is not None:
        items.append(("latency_ms", "", snap.probe.latency_ms))
    for name, data, log in snap.db_sizes or []:
        items.append(("db_size_mb", name, round(data + log, 2)))
    if snap.table_count is not None:
        items.append(("table_count", snap.database, float(snap.table_count)))
    for t in snap.tables or []:
        items.append(("table_rows", f"{snap.database}.{t.schema}.{t.name}", float(t.rows)))
        if t.size_mb is not None:
            items.append(("table_mb", f"{snap.database}.{t.schema}.{t.name}", round(t.size_mb, 3)))
    if snap.requests is not None:
        items.append(("active_requests", "", float(len(snap.requests))))
        items.append(("blocked_requests", "", float(sum(1 for r in snap.requests if r["blocked_by"]))))
    return items
