"""Главная панель: сводка, активные подключения и задачи, последние события, быстрые действия."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from database.connection import STATE_LABELS

from .context import AppContext
from .helpers import open_path
from .widgets import EmptyState, PageHeader, StatCard

STATUS_TEXT = {"ok": "Успешно", "error": "Ошибка", "warning": "Предупреждение", "info": "Информация"}


def make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setAlternatingRowColors(True)
    return t


def fill_table(t: QTableWidget, rows: list[list[str]]) -> None:
    t.setSortingEnabled(False)
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(str(val)))
    t.resizeColumnsToContents()
    t.horizontalHeader().setStretchLastSection(True)


class HomePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self._build()
        ctx.connections_changed.connect(self.refresh)
        ctx.profiles_changed.connect(self.refresh)
        ctx.reports_changed.connect(self.refresh)
        self._recent = []
        ctx.runner.task_changed.connect(lambda _t: self.refresh())
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.setSpacing(14)
        root.addWidget(PageHeader("Главная", "Состояние подключений, фоновые задачи и недавняя активность."))

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.c_profiles = StatCard("Сохранённые подключения")
        self.c_active = StatCard("Активные подключения")
        self.c_tasks = StatCard("Активные задачи")
        self.c_errors = StatCard("Ошибок за 24 часа")
        for c in (self.c_profiles, self.c_active, self.c_tasks, self.c_errors):
            cards.addWidget(c)
        root.addLayout(cards)

        actions = QHBoxLayout()
        for text, key, primary in (("Новое подключение", "connections", True),
                                   ("Обозреватель объектов", "explorer", False),
                                   ("Конвертер файлов", "converter", False),
                                   ("Автоматизация", "automation", False),
                                   ("История задач", "history", False),
                                   ("Настройки", "settings", False)):
            b = QPushButton(text)
            if primary:
                b.setObjectName("Primary")
            b.clicked.connect(lambda _=False, k=key: self.ctx.navigate.emit(k))
            actions.addWidget(b)
        actions.addStretch(1)
        root.addLayout(actions)

        grid = QGridLayout()
        grid.setSpacing(12)
        root.addLayout(grid, 1)

        self.conn_card, self.conn_table = self._card("Подключения", ["Название", "Сервер", "Состояние"])
        self.events_card, self.events_table = self._card("Последние события", ["Время", "Раздел", "Действие", "Статус"])
        self.empty_conn = EmptyState("Подключений пока нет",
                                     "Создайте профиль подключения к SQL Server, чтобы начать работу.",
                                     "Создать подключение")
        self.empty_conn.action_clicked.connect(lambda: self.ctx.navigate.emit("connections"))
        self.reports_card, self.reports_table = self._card("Недавние отчёты (двойной щелчок — открыть)",
                                                           ["Время", "Отчёт", "Файл"])
        self.reports_table.cellDoubleClicked.connect(self._open_report)
        self.no_reports = QLabel("Отчётов пока нет. Сформируйте первый на странице «Отчёты Word».")
        self.no_reports.setObjectName("Muted")
        self.reports_card.layout().addWidget(self.no_reports)
        grid.addWidget(self.conn_card, 0, 0)
        grid.addWidget(self.empty_conn, 0, 0)
        grid.addWidget(self.events_card, 0, 1)
        grid.addWidget(self.reports_card, 1, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 2)
        grid.setRowStretch(1, 1)

    def _open_report(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._recent) and self._recent[row].files:
            open_path(self._recent[row].files[0])

    @staticmethod
    def _card(title: str, headers: list[str]):
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lbl = QLabel(title)
        lbl.setObjectName("CardTitle")
        lay.addWidget(lbl)
        table = make_table(headers)
        lay.addWidget(table)
        return card, table

    def refresh(self) -> None:
        profiles = self.svc.profiles.repo.list()
        manager = self.svc.manager
        primaries = [manager.primary(p.id) for p in profiles]
        active = [c for c in primaries if c is not None]
        tasks = self.svc.tasks.tasks(active_only=True)
        errors = self.svc.oplog.count_since(24, "error")

        self.c_profiles.set_value(str(len(profiles)))
        self.c_active.set_value(f"{len(active)} / {manager.max_connections}",
                                "лимит одновременных подключений" if active else "")
        self.c_tasks.set_value(str(len(tasks)), tasks[0].name if tasks else "нет выполняющихся задач")
        self.c_errors.set_value(str(errors))

        self.empty_conn.setVisible(not profiles)
        self.conn_card.setVisible(bool(profiles))
        fill_table(self.conn_table, [
            [p.name, p.display(), STATE_LABELS[c.state] if c else "Отключено"]
            for p, c in zip(profiles, primaries)])
        self._recent = self.ctx.reports.recent(8)
        self.reports_table.setVisible(bool(self._recent))
        self.no_reports.setVisible(not self._recent)
        fill_table(self.reports_table, [[r.ts.replace("T", " "), r.name, ", ".join(Path(f).name for f in r.files)]
                                        for r in self._recent])
        fill_table(self.events_table, [
            [r.ts.replace("T", " "), r.category, r.action, STATUS_TEXT.get(r.status, r.status)]
            for r in self.svc.oplog.recent(12)])
