"""Небольшой редактор таблицы (строки добавляются/удаляются/перемещаются). Данные — в Python-списке."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)


@dataclass
class Col:
    key: str
    title: str
    kind: str = "text"                              # text | int | check | combo
    choices: list[tuple[str, str]] | None = None    # для combo: (подпись, значение)
    width: int = 0
    default: object = ""


class TableEditor(QWidget):
    changed = Signal()

    def __init__(self, cols: list[Col], parent=None, movable: bool = False, min_height: int = 130):
        super().__init__(parent)
        self.cols = cols
        self._data: list[dict] = []
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels([c.title for c in cols])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(min_height)
        for i, c in enumerate(cols):
            if c.width:
                self.table.setColumnWidth(i, c.width)
        self.table.itemChanged.connect(self._on_item)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        row.setSpacing(4)
        for text, fn, tip in (("＋", self.add_row, "Добавить строку"), ("−", self.remove_row, "Удалить выбранную строку")):
            b = QPushButton(text)
            b.setFixedWidth(34)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        if movable:
            for text, d, tip in (("↑", -1, "Выше"), ("↓", 1, "Ниже")):
                b = QPushButton(text)
                b.setFixedWidth(34)
                b.setToolTip(tip)
                b.clicked.connect(lambda _=False, dd=d: self.move(dd))
                row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

    # ---- данные -------------------------------------------------------
    def _blank(self) -> dict:
        return {c.key: c.default for c in self.cols}

    def set_rows(self, rows: list[dict]) -> None:
        self._loading = True
        self._data = [{**self._blank(), **{k: v for k, v in r.items() if k in {c.key for c in self.cols}}} for r in rows]
        self._render()
        self._loading = False

    def rows(self) -> list[dict]:
        """Строки без пустых (первый столбец текстового типа пуст — строка считается пустой)."""
        first = self.cols[0]
        out = []
        for r in self._data:
            if first.kind == "text" and not str(r.get(first.key, "")).strip():
                continue
            row = {}
            for c in self.cols:
                v = r.get(c.key, c.default)
                if c.kind == "int":
                    try:
                        v = int(str(v).strip() or 0)
                    except ValueError:
                        v = int(c.default or 0)
                elif c.kind == "check":
                    v = bool(v)
                elif c.kind == "text":
                    v = str(v).strip()
                row[c.key] = v
            out.append(row)
        return out

    def _render(self) -> None:
        self.table.setRowCount(len(self._data))
        for r, rec in enumerate(self._data):
            for c, col in enumerate(self.cols):
                v = rec.get(col.key, col.default)
                if col.kind == "combo":
                    box = QComboBox()
                    for label, val in col.choices or []:
                        box.addItem(label, val)
                    box.setCurrentIndex(max(0, box.findData(v)))
                    box.currentIndexChanged.connect(lambda _i, rr=r, cc=col, b=box: self._on_combo(rr, cc, b))
                    self.table.setCellWidget(r, c, box)
                elif col.kind == "check":
                    item = QTableWidgetItem("")
                    item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                    item.setCheckState(Qt.CheckState.Checked if v else Qt.CheckState.Unchecked)
                    self.table.setItem(r, c, item)
                else:
                    self.table.setItem(r, c, QTableWidgetItem(str(v)))

    def _on_item(self, item) -> None:
        if self._loading:
            return
        r, c = item.row(), item.column()
        if not (0 <= r < len(self._data)):
            return
        col = self.cols[c]
        self._data[r][col.key] = (item.checkState() == Qt.CheckState.Checked) if col.kind == "check" else item.text()
        self.changed.emit()

    def _on_combo(self, r: int, col: Col, box) -> None:
        if self._loading or not (0 <= r < len(self._data)):
            return
        self._data[r][col.key] = box.currentData()
        self.changed.emit()

    # ---- действия ------------------------------------------------------
    def add_row(self, values: dict | None = None) -> None:
        self._data.append({**self._blank(), **(values or {})})
        self._loading = True
        self._render()
        self._loading = False
        self.table.setCurrentCell(len(self._data) - 1, 0)
        self.changed.emit()

    def remove_row(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self._data):
            del self._data[r]
            self._loading = True
            self._render()
            self._loading = False
            self.changed.emit()

    def move(self, delta: int) -> None:
        r = self.table.currentRow()
        n = r + delta
        if 0 <= r < len(self._data) and 0 <= n < len(self._data):
            self._data[r], self._data[n] = self._data[n], self._data[r]
            self._loading = True
            self._render()
            self._loading = False
            self.table.setCurrentCell(n, 0)
            self.changed.emit()
