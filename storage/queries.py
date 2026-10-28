"""История выполненных запросов и избранные запросы."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from utils.logging_setup import redact

from .db import AppDatabase

MAX_SQL_CHARS = 200_000


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    ts: str
    connection_name: str
    database_name: str
    sql: str
    status: str
    duration_ms: int | None
    row_count: int | None
    message: str


@dataclass(frozen=True)
class Favorite:
    id: int
    title: str
    sql: str
    created_at: str


class QueryHistory:
    """Текст запроса сохраняется с маскированием паролей/токенов (см. utils.logging_setup.redact)."""

    def __init__(self, db: AppDatabase, limit: int = 500):
        self._db = db
        self.limit = limit

    def add(self, sql: str, status: str, *, connection_name: str = "", database_name: str = "",
            duration_ms: int | None = None, row_count: int | None = None, message: str = "") -> int:
        entry_id = self._db.execute(
            "INSERT INTO query_history(ts, connection_name, database_name, sql, status, duration_ms, row_count, message) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), connection_name, database_name,
             redact(sql)[:MAX_SQL_CHARS], status, duration_ms, row_count, redact(message)[:1000]))
        self._db.execute(
            "DELETE FROM query_history WHERE id NOT IN (SELECT id FROM query_history ORDER BY id DESC LIMIT ?)",
            (self.limit,))
        return entry_id

    def recent(self, limit: int = 200, search: str = "") -> list[HistoryEntry]:
        if search:
            like = "%" + search.replace("%", r"\%").replace("_", r"\_") + "%"
            rows = self._db.query(
                "SELECT * FROM query_history WHERE sql LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?", (like, limit))
        else:
            rows = self._db.query("SELECT * FROM query_history ORDER BY id DESC LIMIT ?", (limit,))
        return [HistoryEntry(r["id"], r["ts"], r["connection_name"], r["database_name"], r["sql"], r["status"],
                             r["duration_ms"], r["row_count"], r["message"]) for r in rows]

    def delete(self, entry_id: int) -> None:
        self._db.execute("DELETE FROM query_history WHERE id = ?", (entry_id,))

    def clear(self) -> None:
        self._db.execute("DELETE FROM query_history")


class FavoriteQueries:
    def __init__(self, db: AppDatabase):
        self._db = db

    def add(self, title: str, sql: str) -> int:
        title = title.strip() or sql.strip().split("\n", 1)[0][:60] or "Запрос"
        return self._db.execute(
            "INSERT INTO favorite_queries(title, sql, created_at) VALUES(?,?,?)",
            (title[:200], redact(sql)[:MAX_SQL_CHARS], datetime.now().isoformat(timespec="seconds")))

    def list(self) -> list[Favorite]:
        return [Favorite(r["id"], r["title"], r["sql"], r["created_at"])
                for r in self._db.query("SELECT * FROM favorite_queries ORDER BY title COLLATE NOCASE")]

    def rename(self, fav_id: int, title: str) -> None:
        if title.strip():
            self._db.execute("UPDATE favorite_queries SET title = ? WHERE id = ?", (title.strip()[:200], fav_id))

    def delete(self, fav_id: int) -> None:
        self._db.execute("DELETE FROM favorite_queries WHERE id = ?", (fav_id,))
