"""Журнал операций (подключения, задачи; позже — отчёты, экспорт, конвертация)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from utils.logging_setup import redact

from .db import AppDatabase

STATUSES = ("ok", "error", "warning", "info")


@dataclass(frozen=True)
class OperationRecord:
    id: int
    ts: str
    category: str
    action: str
    status: str
    message: str
    duration_ms: int | None


class OperationLog:
    def __init__(self, db: AppDatabase):
        self._db = db

    def add(self, category: str, action: str, status: str = "info", message: str = "",
            duration_ms: int | None = None) -> int:
        if status not in STATUSES:
            status = "info"
        ts = datetime.now().isoformat(timespec="seconds")
        return self._db.execute(
            "INSERT INTO operation_log(ts, category, action, status, message, duration_ms) "
            "VALUES(?,?,?,?,?,?)",
            (ts, category, action, status, redact(message)[:2000], duration_ms),
        )

    def recent(self, limit: int = 50, category: str | None = None) -> list[OperationRecord]:
        if category:
            rows = self._db.query(
                "SELECT * FROM operation_log WHERE category = ? ORDER BY id DESC LIMIT ?",
                (category, limit))
        else:
            rows = self._db.query("SELECT * FROM operation_log ORDER BY id DESC LIMIT ?", (limit,))
        return [OperationRecord(r["id"], r["ts"], r["category"], r["action"], r["status"],
                                r["message"], r["duration_ms"]) for r in rows]

    def count_since(self, hours: int = 24, status: str | None = None) -> int:
        since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        if status:
            rows = self._db.query(
                "SELECT COUNT(*) c FROM operation_log WHERE ts >= ? AND status = ?", (since, status))
        else:
            rows = self._db.query("SELECT COUNT(*) c FROM operation_log WHERE ts >= ?", (since,))
        return int(rows[0]["c"])
