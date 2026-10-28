"""История задач и операций (журнал из локальной базы приложения)."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .context import AppContext
from .home_page import STATUS_TEXT, fill_table, make_table
from .widgets import PageHeader

CATEGORIES = [("Все разделы", None), ("Подключения", "connection"), ("Фоновые задачи", "task")]


class HistoryPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader("История задач", "Журнал подключений и фоновых задач. Пароли в журнал не попадают."))
        bar = QHBoxLayout()
        self.category = QComboBox()
        for text, val in CATEGORIES:
            self.category.addItem(text, val)
        self.category.currentIndexChanged.connect(self.refresh)
        self.limit = QComboBox()
        for n in (50, 200, 1000):
            self.limit.addItem(f"Последние {n}", n)
        self.limit.currentIndexChanged.connect(self.refresh)
        btn = QPushButton("Обновить")
        btn.clicked.connect(self.refresh)
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        for w in (self.category, self.limit, btn):
            bar.addWidget(w)
        bar.addWidget(self.count, 1)
        root.addLayout(bar)
        self.table = make_table(["Время", "Раздел", "Действие", "Статус", "Длительность", "Сообщение"])
        root.addWidget(self.table, 1)
        ctx.runner.task_changed.connect(lambda t: None if t.is_active else self.refresh())
        self.refresh()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.refresh()

    def refresh(self, *_args) -> None:
        recs = self.ctx.services.oplog.recent(self.limit.currentData(), self.category.currentData())
        fill_table(self.table, [
            [r.ts.replace("T", " "), r.category, r.action, STATUS_TEXT.get(r.status, r.status),
             f"{r.duration_ms} мс" if r.duration_ms is not None else "—", r.message] for r in recs])
        self.count.setText(f"Записей: {len(recs)}")
