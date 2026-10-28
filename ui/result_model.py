"""Модель таблицы результатов запроса (ленивая, с добавлением порций и сортировкой)."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from utils.formatting import NULL_TEXT, format_cell, is_numeric, sort_key


class ResultTableModel(QAbstractTableModel):
    def __init__(self, columns: list[str], parent=None):
        super().__init__(parent)
        self.columns = list(columns)
        self.rows: list[tuple] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return format_cell(value)
        if role == Qt.ItemDataRole.ToolTipRole:
            return format_cell(value, max_len=1500)
        if role == Qt.ItemDataRole.TextAlignmentRole and is_numeric(value):
            return (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter).value
        if role == Qt.ItemDataRole.ForegroundRole and value is None:
            return QBrush(QColor("#8A93A6"))
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section] if section < len(self.columns) else None
        return str(section + 1)

    def append_rows(self, rows: list[tuple]) -> None:
        if not rows:
            return
        first = len(self.rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        if column < 0 or column >= len(self.columns):
            return
        self.layoutAboutToBeChanged.emit()
        reverse = order == Qt.SortOrder.DescendingOrder
        nulls = [r for r in self.rows if r[column] is None]
        rest = [r for r in self.rows if r[column] is not None]
        rest.sort(key=lambda r: sort_key(r[column]), reverse=reverse)
        self.rows = rest + nulls            # NULL всегда в конце
        self.layoutChanged.emit()

    def cell_text(self, row: int, col: int) -> str:
        v = self.rows[row][col]
        return "" if v is None else format_cell(v, max_len=10 ** 9)
