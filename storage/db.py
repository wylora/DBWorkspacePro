"""Локальная база приложения (SQLite): настройки, профили, журнал операций."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

_MIGRATIONS: list[str] = [
    # версия 1
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE connection_profiles (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        data       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE operation_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        category    TEXT NOT NULL,
        action      TEXT NOT NULL,
        status      TEXT NOT NULL,
        message     TEXT NOT NULL DEFAULT '',
        duration_ms INTEGER
    );
    CREATE INDEX idx_operation_log_ts ON operation_log(ts);
    """,
    # версия 2: история запросов и избранное (SQL-редактор)
    """
    CREATE TABLE query_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        connection_name TEXT NOT NULL DEFAULT '',
        database_name   TEXT NOT NULL DEFAULT '',
        sql             TEXT NOT NULL,
        status          TEXT NOT NULL,
        duration_ms     INTEGER,
        row_count       INTEGER,
        message         TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_query_history_ts ON query_history(ts);
    CREATE TABLE favorite_queries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        sql        TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # версия 3: автоматизация и мониторинг
    """
    CREATE TABLE scheduled_tasks (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        kind       TEXT NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 1,
        schedule   TEXT NOT NULL,
        config     TEXT NOT NULL,
        catch_up   INTEGER NOT NULL DEFAULT 0,
        next_run   TEXT,
        last_run   TEXT,
        last_status TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE task_runs (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id  TEXT NOT NULL,
        started  TEXT NOT NULL,
        finished TEXT,
        status   TEXT NOT NULL,
        trigger  TEXT NOT NULL,
        message  TEXT NOT NULL DEFAULT '',
        details  TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_task_runs_task ON task_runs(task_id, id);
    CREATE TABLE notifications (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        level   TEXT NOT NULL,
        title   TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        source  TEXT NOT NULL DEFAULT '',
        is_read INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE metrics (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        metric     TEXT NOT NULL,
        mkey       TEXT NOT NULL DEFAULT '',
        value      REAL
    );
    CREATE INDEX idx_metrics ON metrics(profile_id, metric, mkey, ts);
    CREATE TABLE script_scenarios (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        data       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE script_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scenario_id TEXT NOT NULL,
        ts          TEXT NOT NULL,
        status      TEXT NOT NULL,
        duration_ms INTEGER,
        rows        INTEGER,
        message     TEXT NOT NULL DEFAULT '',
        details     TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_script_runs ON script_runs(scenario_id, id);
    """,
]

SCHEMA_VERSION = len(_MIGRATIONS)


class AppDatabase:
    """Потокобезопасная обёртка над одним соединением SQLite."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"База данных приложения создана более новой версией (схема {version}, "
                    f"поддерживается {SCHEMA_VERSION})."
                )
            for i in range(version, SCHEMA_VERSION):
                self._conn.executescript("BEGIN;" + _MIGRATIONS[i] + f"PRAGMA user_version = {i + 1};COMMIT;")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Выполняет изменяющий запрос, возвращает lastrowid."""
        with self._lock:
            try:
                cur = self._conn.execute(sql, tuple(params))
                self._conn.commit()
                return cur.lastrowid
            except BaseException:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
