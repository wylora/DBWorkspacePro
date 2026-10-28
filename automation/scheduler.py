"""Планировщик: работает, пока открыта программа. Пропущенные запуски не выполняются задним числом
(если у задачи не включено «выполнить при запуске программы»), а отмечаются в истории."""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Callable

from .schedule import next_run
from .store import Notifications, RunLog, ScheduledTask, TaskStore

log = logging.getLogger("dbworkspace.scheduler")


class Scheduler:
    def __init__(self, store: TaskStore, runs: RunLog, notes: Notifications,
                 submit: Callable[[ScheduledTask, str], bool], clock: Callable[[], dt.datetime] = dt.datetime.now,
                 grace: dt.timedelta = dt.timedelta(minutes=2)):
        self.store, self.runs, self.notes, self.submit, self.clock, self.grace = store, runs, notes, submit, clock, grace
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _iso(d: dt.datetime | None) -> str | None:
        return d.isoformat(timespec="seconds") if d else None

    def refresh(self, task: ScheduledTask, now: dt.datetime | None = None) -> ScheduledTask:
        task.next_run = self._iso(next_run(task.schedule, now or self.clock())) if task.enabled else None
        return task

    def startup(self, now: dt.datetime | None = None) -> None:
        now = now or self.clock()
        interrupted = self.runs.mark_interrupted()
        if interrupted:
            self.notes.add("warning", "Прерванные задачи", f"Запусков, прерванных закрытием программы: {interrupted}.",
                           "scheduler")
        with self._lock:
            for t in self.store.list():
                if not t.enabled:
                    continue
                due = dt.datetime.fromisoformat(t.next_run) if t.next_run else None
                if due is not None and due < now - self.grace:
                    if t.catch_up:
                        self.refresh(t, now)
                        self.store.save(t)
                        self.submit(t, "catch_up")
                        continue
                    self.runs.add(t.id, "skipped", "missed",
                                  f"Пропущен запуск {due:%Y-%m-%d %H:%M}: программа в это время не работала.")
                    self.notes.add("warning", f"Пропущен запуск: {t.name}",
                                   f"Плановое время {due:%Y-%m-%d %H:%M}. Программа в это время не работала.", "scheduler")
                if due is None or due < now - self.grace:
                    self.refresh(t, now)
                    self.store.save(t)

    def tick(self, now: dt.datetime | None = None) -> list[str]:
        now = now or self.clock()
        started = []
        with self._lock:
            for t in self.store.list():
                if not t.enabled or not t.next_run:
                    continue
                if dt.datetime.fromisoformat(t.next_run) > now:
                    continue
                self.refresh(t, now)                 # следующий запуск считаем до выполнения: долгая задача не задвоится
                self.store.save(t)
                if self.submit(t, "schedule"):
                    started.append(t.id)
                else:
                    self.runs.add(t.id, "skipped", "schedule", "Пропущено: предыдущий запуск ещё выполняется.")
        return started

    def on_connect(self, profile_id: str) -> list[str]:
        started = []
        for t in self.store.list():
            if t.enabled and t.schedule.kind == "on_connect" and t.config.get("profile_id") == profile_id:
                if self.submit(t, "on_connect"):
                    started.append(t.id)
        return started

    def start(self, interval: float = 15.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка планировщика")

        self._thread = threading.Thread(target=loop, name="dbwp-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
