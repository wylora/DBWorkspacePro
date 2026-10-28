"""Страница-заглушка для разделов, которые ещё не реализованы (честная пометка статуса)."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .widgets import EmptyState, PageHeader


class PlaceholderPage(QWidget):
    def __init__(self, title: str, stage: int, planned: str, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(title))
        root.addWidget(EmptyState(
            "Раздел ещё не реализован",
            f"Запланировано на этап {stage}.\n\n{planned}\n\nНа текущем этапе здесь нет рабочих функций — "
            "кнопки намеренно не добавлены, чтобы не имитировать готовность.",
            badge=f"Этап {stage}"), 1)
        root.addStretch(0)
