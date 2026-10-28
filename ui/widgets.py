"""Небольшие общие виджеты: карточка статистики, пустое состояние, заголовок страницы."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("PageTitle")
        lay.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("PageSubtitle")
            s.setWordWrap(True)
            lay.addWidget(s)


class StatCard(QFrame):
    def __init__(self, title: str, value: str = "—", hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(2)
        self._title = QLabel(title)
        self._title.setObjectName("CardTitle")
        self._value = QLabel(value)
        self._value.setObjectName("CardValue")
        self._hint = QLabel(hint)
        self._hint.setObjectName("Muted")
        self._hint.setWordWrap(True)
        for w in (self._title, self._value, self._hint):
            lay.addWidget(w)

    def set_value(self, value: str, hint: str = "") -> None:
        self._value.setText(value)
        self._hint.setText(hint)


class EmptyState(QFrame):
    """Понятное пустое состояние: заголовок, пояснение и (необязательно) кнопка действия."""

    action_clicked = Signal()

    def __init__(self, title: str, description: str, action_text: str = "", badge: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 32, 32, 32)
        lay.setSpacing(10)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if badge:
            b = QLabel(badge)
            b.setObjectName("Badge")
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignCenter)
        t = QLabel(title)
        t.setObjectName("PageTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setWordWrap(True)
        d = QLabel(description)
        d.setObjectName("Muted")
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(d)
        if action_text:
            btn = QPushButton(action_text)
            btn.setObjectName("Primary")
            btn.clicked.connect(self.action_clicked.emit)
            lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
