"""Хранилище автоматизации: задачи по расписанию, история запусков, уведомления, метрики, SQL-сценарии."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import asdict, dataclass, field

from storage.db import AppDatabase
from utils.logging_setup import redact

from .schedule import Schedule

TASK_KINDS = {"report": "Формирование отчёта", "export": "Экспорт таблиц", "backup": "Резервное копирование",
              "health": "Проверка доступности сервера", "table_stats": "Сбор размеров таблиц",
              "script": "SQL-сценарий"}


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class ScheduledTask:
    id: str
    name: str
    kind: str
    schedule: Schedule
    config: dict = field(default_factory=dict)
    enabled: bool = True
    catch_up: bool = False                  # выполнить один раз при запуске программы, если запуск был пропущен
    next_run: str | None = None
    last_run: str | None = None
    last_status: str | None = None

    @staticmethod
    def new(name: str, kind: str, schedule: Schedule, config: dict | None = None) -> "ScheduledTask":
        return ScheduledTask(uuid.uuid4().hex[:12], name, kind, schedule, dict(config or {}))


@dataclass(frozen=True)
class RunRecord:
    id: int
    task_id: str
    started: str
    finished: str | None
    status: str
    trigger: str
    message: str
    details: str


@dataclass(frozen=True)
class Notification:
    id: int
    ts: str
    level: str
    title: str
    message: str
    source: str
    is_read: bool


class TaskStore:
    def __init__(self, db: AppDatabase):
        self.db = db

    @staticmethod
    def _row(r) -> ScheduledTask:
        return ScheduledTask(r["id"], r["name"], r["kind"], Schedule.from_dict(json.loads(r["schedule"])),
                             json.loads(r["config"]), bool(r["enabled"]), bool(r["catch_up"]), r["next_run"],
                             r["last_run"], r["last_status"])

    def list(self) -> list[ScheduledTask]:
        return [self._row(r) for r in self.db.query("SELECT * FROM scheduled_tasks ORDER BY name COLLATE NOCASE")]

    def get(self, task_id: str) -> ScheduledTask | None:
        rows = self.db.query("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        return self._row(rows[0]) if rows else None

    def save(self, t: ScheduledTask) -> None:
        data = (t.name, t.kind, int(t.enabled), json.dumps(t.schedule.to_dict()), json.dumps(t.config, ensure_ascii=False),
                int(t.catch_up), t.next_run, t.last_run, t.last_status, now_iso())
        if self.get(t.id) is None:
            self.db.execute("INSERT INTO scheduled_tasks(name, kind, enabled, schedule, config, catch_up, next_run, last_run, "
                            "last_status, updated_at, id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            data + (t.id, now_iso()))
        else:
            self.db.execute("UPDATE scheduled_tasks SET name=?, kind=?, enabled=?, schedule=?, config=?, catch_up=?, "
                            "next_run=?, last_run=?, last_status=?, updated_at=? WHERE id=?", data + (t.id,))

    def delete(self, task_id: str) -> None:
        self.db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        self.db.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))


class RunLog:
    def __init__(self, db: AppDatabase, keep_per_task: int = 200):
        self.db = db
        self.keep = keep_per_task

    def start(self, task_id: str, trigger: str) -> int:
        return self.db.execute("INSERT INTO task_runs(task_id, started, status, trigger) VALUES(?,?,?,?)",
                               (task_id, now_iso(), "running", trigger))

    def finish(self, run_id: int, status: str, message: str = "", details: str = "") -> None:
        self.db.execute("UPDATE task_runs SET finished=?, status=?, message=?, details=? WHERE id=?",
                        (now_iso(), status, redact(message)[:2000], redact(details)[:20000], run_id))
        row = self.db.query("SELECT task_id FROM task_runs WHERE id=?", (run_id,))
        if row:
            self.db.execute("DELETE FROM task_runs WHERE task_id=? AND id NOT IN (SELECT id FROM task_runs WHERE task_id=? "
                            "ORDER BY id DESC LIMIT ?)", (row[0]["task_id"], row[0]["task_id"], self.keep))

    def add(self, task_id: str, status: str, trigger: str, message: str) -> int:
        rid = self.start(task_id, trigger)
        self.finish(rid, status, message)
        return rid

    def recent(self, task_id: str | None = None, limit: int = 50) -> list[RunRecord]:
        if task_id:
            rows = self.db.query("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))
        else:
            rows = self.db.query("SELECT * FROM task_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [RunRecord(r["id"], r["task_id"], r["started"], r["finished"], r["status"], r["trigger"], r["message"],
                          r["details"]) for r in rows]

    def mark_interrupted(self) -> int:
        """Запуски, оставшиеся «выполняется» после аварийного завершения программы."""
        rows = self.db.query("SELECT id FROM task_runs WHERE status='running'")
        for r in rows:
            self.finish(r["id"], "failed", "Выполнение прервано: программа была закрыта или завершилась аварийно.")
        return len(rows)


class Notifications:
    LEVELS = ("info", "success", "warning", "error")

    def __init__(self, db: AppDatabase, keep: int = 500):
        self.db = db
        self.keep = keep
        self.listeners: list = []

    def add(self, level: str, title: str, message: str = "", source: str = "") -> int:
        level = level if level in self.LEVELS else "info"
        nid = self.db.execute("INSERT INTO notifications(ts, level, title, message, source) VALUES(?,?,?,?,?)",
                              (now_iso(), level, redact(title)[:300], redact(message)[:4000], source))
        self.db.execute("DELETE FROM notifications WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT ?)",
                        (self.keep,))
        for cb in list(self.listeners):
            try:
                cb(nid)
            except Exception:  # noqa: BLE001
                pass
        return nid

    def recent(self, limit: int = 100, unread_only: bool = False) -> list[Notification]:
        sql = "SELECT * FROM notifications" + (" WHERE is_read=0" if unread_only else "") + " ORDER BY id DESC LIMIT ?"
        return [Notification(r["id"], r["ts"], r["level"], r["title"], r["message"], r["source"], bool(r["is_read"]))
                for r in self.db.query(sql, (limit,))]

    def unread_count(self) -> int:
        return int(self.db.query("SELECT COUNT(*) c FROM notifications WHERE is_read=0")[0]["c"])

    def mark_all_read(self) -> None:
        self.db.execute("UPDATE notifications SET is_read=1 WHERE is_read=0")

    def clear(self) -> None:
        self.db.execute("DELETE FROM notifications")


class MetricStore:
    def __init__(self, db: AppDatabase, keep_days: int = 90):
        self.db = db
        self.keep_days = keep_days

    def add_many(self, profile_id: str, items: list[tuple[str, str, float | None]], ts: str | None = None) -> None:
        ts = ts or now_iso()
        for metric, key, value in items:
            self.db.execute("INSERT INTO metrics(ts, profile_id, metric, mkey, value) VALUES(?,?,?,?,?)",
                            (ts, profile_id, metric, key or "", value))
        cutoff = (dt.datetime.now() - dt.timedelta(days=self.keep_days)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))

    def series(self, profile_id: str, metric: str, key: str = "", limit: int = 500) -> list[tuple[str, float | None]]:
        rows = self.db.query("SELECT ts, value FROM (SELECT ts, value, id FROM metrics WHERE profile_id=? AND metric=? "
                             "AND mkey=? ORDER BY id DESC LIMIT ?) ORDER BY id", (profile_id, metric, key or "", limit))
        return [(r["ts"], r["value"]) for r in rows]

    def latest(self, profile_id: str, metric: str) -> dict[str, tuple[str, float | None]]:
        rows = self.db.query("SELECT mkey, ts, value FROM metrics m WHERE profile_id=? AND metric=? AND id = (SELECT MAX(id) "
                             "FROM metrics WHERE profile_id=m.profile_id AND metric=m.metric AND mkey=m.mkey)",
                             (profile_id, metric))
        return {r["mkey"]: (r["ts"], r["value"]) for r in rows}
