"""Диалоги «История запросов» и «Избранное»."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from bootstrap import AppServices

STATUS = {"ok": "Успешно", "error": "Ошибка", "cancelled": "Отменён"}


def _preview(text: str, n: int = 90) -> str:
    line = " ".join(text.split())
    return line[:n] + ("…" if len(line) > n else "")


class HistoryDialog(QDialog):
    """on_open(sql, new_tab) вызывается при выборе запроса; on_favorite(sql) — «в избранное»."""

    def __init__(self, parent, services: AppServices, on_open, on_favorite):
        super().__init__(parent)
        self.setWindowTitle("История запросов")
        self.resize(940, 560)
        self.svc, self._on_open, self._on_fav = services, on_open, on_favorite
        self._entries = []

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по тексту запроса…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.reload)
        top.addWidget(self.search, 1)
        if not self.svc.settings.get("query_history_enabled"):
            off = QLabel("Сохранение истории выключено в настройках.")
            off.setObjectName("Muted")
            top.addWidget(off)
        lay.addLayout(top)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Время", "Подключение", "База", "Статус", "Время, мс", "Запрос"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.cellDoubleClicked.connect(lambda *_: self._open(False))
        split.addWidget(self.table)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        split.addWidget(self.view)
        split.setSizes([320, 180])
        lay.addWidget(split, 1)

        row = QHBoxLayout()
        for text, fn in (("Вставить в текущую вкладку", lambda: self._open(False)),
                         ("Открыть в новой вкладке", lambda: self._open(True)),
                         ("В избранное", self._favorite), ("Удалить", self._delete),
                         ("Очистить историю", self._clear)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.reload()

    def reload(self, *_args) -> None:
        self._entries = self.svc.history.recent(500, self.search.text().strip())
        self.table.setRowCount(len(self._entries))
        for r, e in enumerate(self._entries):
            vals = [e.ts.replace("T", " "), e.connection_name, e.database_name, STATUS.get(e.status, e.status),
                    "" if e.duration_ms is None else str(e.duration_ms), _preview(e.sql)]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.view.clear()

    def _current(self):
        row = self.table.currentRow()
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def _on_select(self) -> None:
        e = self._current()
        self.view.setPlainText(e.sql + (f"\n\n-- {e.message}" if e and e.message else "") if e else "")

    def _open(self, new_tab: bool) -> None:
        e = self._current()
        if e:
            self._on_open(e.sql, new_tab)
            self.accept()

    def _favorite(self) -> None:
        e = self._current()
        if e:
            self._on_fav(e.sql)

    def _delete(self) -> None:
        e = self._current()
        if e:
            self.svc.history.delete(e.id)
            self.reload()

    def _clear(self) -> None:
        answer = QMessageBox.question(self, "Очистить историю", "Удалить всю историю запросов?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.svc.history.clear()
            self.reload()


class FavoritesDialog(QDialog):
    def __init__(self, parent, services: AppServices, on_open):
        super().__init__(parent)
        self.setWindowTitle("Избранные запросы")
        self.resize(820, 520)
        self.svc, self._on_open = services, on_open
        self._items = []

        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Название", "Добавлено"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.cellDoubleClicked.connect(lambda *_: self._open(False))
        split.addWidget(self.table)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        split.addWidget(self.view)
        split.setSizes([300, 500])
        lay.addWidget(split, 1)
        self.empty = QLabel("Избранных запросов пока нет. Добавьте текущий запрос кнопкой «★ В избранное».")
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)

        row = QHBoxLayout()
        for text, fn in (("Вставить в текущую вкладку", lambda: self._open(False)),
                         ("Открыть в новой вкладке", lambda: self._open(True)),
                         ("Переименовать", self._rename), ("Удалить", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.reload()

    def reload(self) -> None:
        self._items = self.svc.favorites.list()
        self.table.setRowCount(len(self._items))
        for r, f in enumerate(self._items):
            self.table.setItem(r, 0, QTableWidgetItem(f.title))
            self.table.setItem(r, 1, QTableWidgetItem(f.created_at.replace("T", " ")))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.empty.setVisible(not self._items)
        self.view.clear()

    def _current(self):
        row = self.table.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _on_select(self) -> None:
        f = self._current()
        self.view.setPlainText(f.sql if f else "")

    def _open(self, new_tab: bool) -> None:
        f = self._current()
        if f:
            self._on_open(f.sql, new_tab)
            self.accept()

    def _rename(self) -> None:
        f = self._current()
        if not f:
            return
        text, ok = QInputDialog.getText(self, "Переименовать", "Название:", QLineEdit.EchoMode.Normal, f.title)
        if ok and text.strip():
            self.svc.favorites.rename(f.id, text)
            self.reload()

    def _delete(self) -> None:
        f = self._current()
        if f:
            self.svc.favorites.delete(f.id)
            self.reload()
