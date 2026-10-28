"""Общий контекст интерфейса: сервисы, запуск задач и сигналы между страницами."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from bootstrap import AppServices
from automation.service import AutomationService
from converter.service import ConverterService
from database.connection import ConnectionState
from reports.service import ReportService

from .qt_bridge import QtTaskRunner


class AppContext(QObject):
    connections_changed = Signal()   # изменилось состояние/набор подключений
    profiles_changed = Signal()      # изменился список сохранённых профилей
    theme_changed = Signal(str)
    navigate = Signal(str)           # ключ страницы
    message = Signal(str)            # сообщение в строку состояния
    settings_changed = Signal()      # изменены настройки (шрифт редактора и т. п.)
    open_template = Signal(str)      # открыть шаблон в конструкторе (id шаблона)
    reports_changed = Signal()       # сформирован отчёт (обновить «недавние»)
    notifications_changed = Signal() # новое уведомление
    automation_changed = Signal(str) # завершён запуск задачи (id задачи)
    open_sql = Signal(str)           # открыть текст в новой вкладке SQL-редактора

    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self.runner = QtTaskRunner(services.tasks, self)
        self.reports = ReportService(services)
        self.converter = ConverterService(services)
        self.automation = AutomationService(services)
        self.automation.notes.listeners.append(lambda _nid: self.notifications_changed.emit())
        self.automation.listeners.append(lambda tid: self.automation_changed.emit(tid))

        def on_state(conn, old, new):
            if new is ConnectionState.CONNECTED and old is not ConnectionState.LOST and conn.label == "primary":
                self.automation.scheduler.on_connect(conn.profile.id)
        services.manager.add_state_listener(on_state)
        services.manager.add_state_listener(lambda conn, old, new: self.connections_changed.emit())

    @property
    def theme(self) -> str:
        return self.services.settings.get("theme")
