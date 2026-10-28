"""Единый механизм фоновых задач (без зависимости от Qt).

Возможности: очередь, лимит параллельных задач, прогресс/этап, кооперативная
отмена, запрет дублей по ключу, хук отмены (например, отмена SQL-запроса),
слушатели состояния и запись итогов через recorder.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .logging_setup import redact

log = logging.getLogger("dbworkspace.tasks")


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = (TaskState.QUEUED, TaskState.RUNNING)


class TaskCancelled(Exception):
    """Бросается задачей при обнаружении отмены."""


class DuplicateTaskError(RuntimeError):
    """Задача с таким ключом уже выполняется или ожидает."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TaskCancelled()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass
class TaskInfo:
    id: int
    name: str
    key: str | None = None
    state: TaskState = TaskState.QUEUED
    stage: str = ""
    progress: float | None = None  # None = неопределённый индикатор
    error: str | None = None
    result: Any = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_token: CancelToken = field(default_factory=CancelToken, repr=False)
    on_cancel: Callable[[], None] | None = field(default=None, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)


class TaskContext:
    """Передаётся в функцию задачи."""

    def __init__(self, manager: "TaskManager", task: TaskInfo):
        self._manager = manager
        self.task = task
        self.cancel_token = task.cancel_token

    def report(self, stage: str | None = None, progress: float | None = None) -> None:
        if stage is not None:
            self.task.stage = stage
        self.task.progress = None if progress is None else max(0.0, min(1.0, progress))
        self._manager._notify(self.task)

    def check_cancelled(self) -> None:
        self.cancel_token.raise_if_cancelled()


class TaskManager:
    def __init__(
        self,
        max_parallel: int = 3,
        recorder: Callable[[TaskInfo], None] | None = None,
        keep_finished: int = 200,
    ) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._recorder = recorder
        self._keep_finished = keep_finished
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._tasks: dict[int, TaskInfo] = {}
        self._pending: deque[tuple[TaskInfo, Callable[[TaskContext], Any]]] = deque()
        self._threads: set[threading.Thread] = set()
        self._running = 0
        self._closed = False
        self._listeners: list[Callable[[TaskInfo], None]] = []

    # --- конфигурация -------------------------------------------------
    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def set_max_parallel(self, n: int) -> None:
        with self._lock:
            self._max_parallel = max(1, int(n))
        self._dispatch()

    def add_listener(self, cb: Callable[[TaskInfo], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    # --- запуск -------------------------------------------------------
    def submit(
        self,
        name: str,
        func: Callable[[TaskContext], Any],
        *,
        key: str | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> TaskInfo:
        with self._lock:
            if self._closed:
                raise RuntimeError("Менеджер задач остановлен")
            if key is not None:
                for t in self._tasks.values():
                    if t.key == key and t.is_active:
                        raise DuplicateTaskError(f"Задача «{name}» уже выполняется")
            task = TaskInfo(id=next(self._ids), name=name, key=key, on_cancel=on_cancel)
            self._tasks[task.id] = task
            self._pending.append((task, func))
        self._notify(task)
        self._dispatch()
        return task

    def _dispatch(self) -> None:
        to_start: list[tuple[TaskInfo, Callable]] = []
        with self._lock:
            while self._pending and self._running < self._max_parallel:
                task, func = self._pending.popleft()
                if task.state is not TaskState.QUEUED:
                    continue  # отменена в очереди
                task.state = TaskState.RUNNING
                task.started_at = time.time()
                self._running += 1
                to_start.append((task, func))
        for task, func in to_start:
            self._notify(task)
            th = threading.Thread(
                target=self._run, args=(task, func), daemon=True, name=f"dbwp-task-{task.id}"
            )
            with self._lock:
                self._threads.add(th)
            th.start()

    def _run(self, task: TaskInfo, func: Callable[[TaskContext], Any]) -> None:
        final = TaskState.SUCCEEDED
        try:
            task.cancel_token.raise_if_cancelled()
            task.result = func(TaskContext(self, task))
        except TaskCancelled:
            final = TaskState.CANCELLED
        except Exception as exc:  # noqa: BLE001
            final = TaskState.FAILED
            task.error = redact(str(exc)) or exc.__class__.__name__
            task.result = exc
            log.exception("Задача «%s» завершилась ошибкой", task.name)
        finally:
            with self._lock:
                task.state = final
                task.finished_at = time.time()
                self._running -= 1
                self._threads.discard(threading.current_thread())
                self._prune()
            task._done.set()
            self._notify(task)
            if self._recorder:
                try:
                    self._recorder(task)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка записи итога задачи")
            self._dispatch()

    def _prune(self) -> None:
        finished = [t for t in self._tasks.values() if not t.is_active]
        extra = len(finished) - self._keep_finished
        if extra > 0:
            for t in sorted(finished, key=lambda x: x.id)[:extra]:
                self._tasks.pop(t.id, None)

    # --- отмена -------------------------------------------------------
    def cancel(self, task_id: int) -> bool:
        hook = None
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or not task.is_active:
                return False
            was_queued = task.state is TaskState.QUEUED
            task.cancel_token.cancel()
            if was_queued:
                task.state = TaskState.CANCELLED
                task.finished_at = time.time()
                task._done.set()
            else:
                hook = task.on_cancel
        if hook:
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка хука отмены")
        self._notify(task)
        if was_queued and self._recorder:
            try:
                self._recorder(task)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка записи итога задачи")
        return True

    # --- чтение -------------------------------------------------------
    def get(self, task_id: int) -> TaskInfo | None:
        with self._lock:
            return self._tasks.get(task_id)

    def tasks(self, active_only: bool = False) -> list[TaskInfo]:
        with self._lock:
            items = list(self._tasks.values())
        if active_only:
            items = [t for t in items if t.is_active]
        return sorted(items, key=lambda t: t.id)

    def _notify(self, task: TaskInfo) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(task)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка слушателя задач")

    # --- завершение ---------------------------------------------------
    def shutdown(self, cancel_running: bool = True, timeout: float = 5.0) -> None:
        with self._lock:
            self._closed = True
            active = [t.id for t in self._tasks.values() if t.is_active]
        if cancel_running:
            for tid in active:
                self.cancel(tid)
        deadline = time.time() + timeout
        with self._lock:
            threads = list(self._threads)
        for th in threads:
            th.join(max(0.0, deadline - time.time()))
