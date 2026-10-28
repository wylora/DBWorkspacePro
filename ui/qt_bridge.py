"""Мост между фоновыми задачами (utils.tasks) и потоком интерфейса Qt.

Колбэки on_success/on_error/on_cancelled всегда вызываются в GUI-потоке
(через сигналы Qt). submit() нужно вызывать из GUI-потока: тогда обработчик
завершения не может сработать раньше, чем зарегистрированы колбэки.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from utils.tasks import TaskInfo, TaskManager, TaskState

log = logging.getLogger("dbworkspace.ui")


class QtTaskRunner(QObject):
    task_changed = Signal(object)   # TaskInfo — любое изменение состояния/прогресса
    _finished = Signal(int)

    def __init__(self, manager: TaskManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._callbacks: dict[int, tuple] = {}
        self._finished.connect(self._on_finished)
        manager.add_listener(self._on_task)

    def _on_task(self, task: TaskInfo) -> None:  # вызывается из любого потока
        self.task_changed.emit(task)
        if not task.is_active:
            self._finished.emit(task.id)

    def submit(self, name: str, func: Callable[[Any], Any], *,
               on_success: Callable[[Any], None] | None = None,
               on_error: Callable[[str], None] | None = None,
               on_cancelled: Callable[[], None] | None = None,
               key: str | None = None,
               on_cancel: Callable[[], None] | None = None) -> TaskInfo:
        task = self.manager.submit(name, func, key=key, on_cancel=on_cancel)
        self._callbacks[task.id] = (task, on_success, on_error, on_cancelled)
        return task

    def _on_finished(self, task_id: int) -> None:
        entry = self._callbacks.pop(task_id, None)
        if entry is None:
            return
        task, on_success, on_error, on_cancelled = entry
        try:
            if task.state is TaskState.SUCCEEDED and on_success:
                on_success(task.result)
            elif task.state is TaskState.FAILED and on_error:
                on_error(task.error or "Неизвестная ошибка")
            elif task.state is TaskState.CANCELLED and on_cancelled:
                on_cancelled()
        except RuntimeError:
            # виджет уже удалён (например, обозреватель перестроен) — результат не нужен
            log.debug("Колбэк задачи пропущен: виджет удалён")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в обработчике завершения задачи")
