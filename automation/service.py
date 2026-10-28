"""Связка автоматизации с приложением: задачи, фоновое выполнение, история, уведомления."""
from __future__ import annotations

import datetime as dt
import logging
import threading

from bootstrap import AppServices
from utils.locks import ProcessLock
from .jobs import JobContext, Outcome, execute, validate_task
from .scheduler import Scheduler
from .scripts import ScenarioStore
from .snapshots import SnapshotStore
from .store import TASK_KINDS, MetricStore, Notifications, RunLog, ScheduledTask, TaskStore

log = logging.getLogger("dbworkspace.automation")
LEVEL = {"ok": "success", "warning": "warning", "failed": "error", "cancelled": "warning"}
STATUS_RU = {"ok": "Успешно", "warning": "С замечаниями", "failed": "Ошибка", "cancelled": "Отменено",
             "skipped": "Пропущено", "running": "Выполняется"}


class AutomationService:
    def __init__(self, services: AppServices, *, background: bool = True, clock=dt.datetime.now):
        db = services.db
        self.svc = services
        self.background = background
        self.tasks = TaskStore(db)
        self.runs = RunLog(db)
        self.notes = Notifications(db)
        self.metrics = MetricStore(db)
        self.scenarios = ScenarioStore(db)
        self.snapshots = SnapshotStore(services.paths.data / "snapshots")
        self.scheduler = Scheduler(self.tasks, self.runs, self.notes, self._submit, clock)
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._plocks: dict[str, ProcessLock] = {}
        self.listeners: list = []           # cb(task_id) после завершения запуска

    # ---- задачи --------------------------------------------------------
    def validate(self, t: ScheduledTask) -> list[str]:
        return validate_task(t, self.svc, self.scenarios)

    def save_task(self, t: ScheduledTask) -> list[str]:
        errs = self.validate(t)
        if errs:
            return errs
        self.scheduler.refresh(t)
        self.tasks.save(t)
        return []

    def set_enabled(self, task_id: str, enabled: bool) -> None:
        t = self.tasks.get(task_id)
        if t:
            t.enabled = enabled
            self.scheduler.refresh(t)
            self.tasks.save(t)

    def delete_task(self, task_id: str) -> None:
        self.tasks.delete(task_id)

    def is_running(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._running

    # ---- запуск ---------------------------------------------------------
    def run_now(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        return bool(t) and self._submit(t, "manual")

    def _acquire(self, task_id: str) -> bool:
        """Задача не выполняется одновременно в двух процессах (окно и Планировщик Windows, две копии программы)."""
        with self._lock:
            if task_id in self._running:
                return False
            lock = ProcessLock(self.svc.paths.locks / f"task-{task_id}.lock")
            if not lock.try_acquire():
                return False
            self._running.add(task_id)
            self._plocks[task_id] = lock
            return True

    def _release(self, task_id: str) -> None:
        with self._lock:
            self._running.discard(task_id)
            lock = self._plocks.pop(task_id, None)
        if lock is not None:
            lock.release()

    def _submit(self, t: ScheduledTask, trigger: str) -> bool:
        if not self._acquire(t.id):
            return False
        if not self.background:
            self._execute(t, trigger, None)
            return True
        try:
            self.svc.tasks.submit(f"Задача: {t.name}", lambda c: self._execute(t, trigger, c), key=f"sched:{t.id}",
                                  )
        except Exception:  # noqa: BLE001
            self._release(t.id)
            return False
        return True

    def _execute(self, t: ScheduledTask, trigger: str, task_ctx) -> Outcome:
        rid = self.runs.start(t.id, trigger)
        prev = t.last_status
        try:
            ctx = JobContext(self.svc, self.metrics, self.scenarios, self.snapshots,
                             cancel_token=task_ctx.cancel_token if task_ctx else None,
                             progress=(lambda s, f: task_ctx.report(s, f)) if task_ctx else (lambda s, f: None),
                             trigger=trigger, previous_status=prev)
            outcome = execute(t, ctx)
        except Exception as exc:  # noqa: BLE001  — execute не бросает, но страхуемся
            log.exception("Сбой выполнения задачи")
            outcome = Outcome("failed", f"{exc.__class__.__name__}: {exc}")
        finally:
            self._release(t.id)
        self.runs.finish(rid, outcome.status, outcome.message, outcome.details)
        current = self.tasks.get(t.id)
        if current is not None:                       # задачу могли изменить, пока она выполнялась
            current.last_run = dt.datetime.now().isoformat(timespec="seconds")
            current.last_status = outcome.status
            self.tasks.save(current)
        policy = t.config.get("notify", "errors")
        notify = outcome.notify if outcome.notify is not None else (
            policy == "always" or (policy == "errors" and outcome.status in ("failed", "warning")))
        if notify and policy != "never":
            self.notes.add(LEVEL.get(outcome.status, "info"), f"{t.name}: {STATUS_RU.get(outcome.status, outcome.status)}",
                           outcome.message, f"task:{t.id}")
        self.svc.oplog.add("automation", f"{TASK_KINDS.get(t.kind, t.kind)}: {t.name}",
                           {"ok": "ok", "failed": "error"}.get(outcome.status, "warning"), outcome.message)
        for cb in list(self.listeners):
            try:
                cb(t.id)
            except Exception:  # noqa: BLE001
                pass
        return outcome

    def execute_sync(self, task_id: str, trigger: str = "cli") -> Outcome:
        t = self.tasks.get(task_id)
        if t is None:
            return Outcome("failed", "Задача не найдена.")
        if not self._acquire(t.id):
            self.runs.add(t.id, "skipped", trigger, "Пропущено: задача уже выполняется (в этой или другой копии программы).")
            return Outcome("failed", "Задача уже выполняется (в этой или другой копии программы).")
        return self._execute(t, trigger, None)

    # ---- планировщик ---------------------------------------------------
    def start(self, interval: float = 15.0) -> None:
        self.scheduler.startup()
        if self.svc.settings.get("scheduler_enabled"):
            self.scheduler.start(interval)

    def stop(self) -> None:
        self.scheduler.stop()
