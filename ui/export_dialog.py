"""Диалог экспорта результата: формат, файл, разделитель, защита от формул, объём выгрузки."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QVBoxLayout)

from utils.export import EXTENSIONS

FORMAT_ITEMS = [("CSV (разделённый текст)", "csv"), ("Excel (.xlsx)", "xlsx"), ("JSON", "json")]
FILTERS = {"csv": "CSV (*.csv)", "xlsx": "Excel (*.xlsx)", "json": "JSON (*.json)"}
DELIMS = [("; (точка с запятой — для русского Excel)", ";"), (", (запятая)", ","), ("Табуляция", "\t")]


class ExportDialog(QDialog):
    def __init__(self, parent, *, settings, shown_rows: int, truncated: bool, full_allowed: bool,
                 full_reason: str = "", default_name: str = "result"):
        super().__init__(parent)
        self.setWindowTitle("Экспорт результата")
        self.setMinimumWidth(560)
        self.params: dict | None = None
        self._default_name = default_name

        form = QFormLayout()
        self.fmt = QComboBox()
        for text, val in FORMAT_ITEMS:
            self.fmt.addItem(text, val)
        self.fmt.currentIndexChanged.connect(self._on_format)
        form.addRow("Формат", self.fmt)

        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("Куда сохранить файл")
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow("Файл", row)

        self.delim = QComboBox()
        for text, val in DELIMS:
            self.delim.addItem(text, val)
        self.delim.setCurrentIndex(max(0, self.delim.findData(settings.get("export_csv_delimiter"))))
        self.delim_label = QLabel("Разделитель")
        form.addRow(self.delim_label, self.delim)

        self.guard = QCheckBox("Защитить от выполнения формул в Excel (значения с =, +, -, @ в начале)")
        self.guard.setChecked(settings.get("export_formula_guard"))
        self.guard.setToolTip("В CSV перед такими значениями добавляется апостроф. В Excel-файле они всегда "
                              "записываются как текст.")
        form.addRow("", self.guard)

        self.r_shown = QRadioButton(f"Только показанные строки ({shown_rows:,})".replace(",", " "))
        self.r_full = QRadioButton("Все строки запроса (повторное выполнение на отдельном соединении, потоком)")
        self.r_shown.setChecked(True)
        self.r_full.setEnabled(full_allowed)
        if not full_allowed:
            self.r_full.setToolTip(full_reason)
        elif truncated:
            self.r_full.setChecked(True)
        scope = QVBoxLayout()
        scope.addWidget(self.r_shown)
        scope.addWidget(self.r_full)
        if truncated:
            warn = QLabel("Показаны не все строки: в таблице действует ограничение предпросмотра.")
            warn.setObjectName("Muted")
            warn.setWordWrap(True)
            scope.addWidget(warn)
        if not full_allowed and full_reason:
            why = QLabel(full_reason)
            why.setObjectName("Muted")
            why.setWordWrap(True)
            scope.addWidget(why)
        form.addRow("Объём", scope)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self._on_format()

    def _fmt(self) -> str:
        return self.fmt.currentData()

    def _on_format(self) -> None:
        is_csv = self._fmt() == "csv"
        self.delim.setVisible(is_csv)
        self.delim_label.setVisible(is_csv)
        p = self.path.text().strip()
        if p:
            self.path.setText(str(Path(p).with_suffix(EXTENSIONS[self._fmt()])))

    def _browse(self) -> None:
        start = self.path.text().strip() or self._default_name + EXTENSIONS[self._fmt()]
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить как", start, FILTERS[self._fmt()])
        if path:
            self.path.setText(str(Path(path).with_suffix(EXTENSIONS[self._fmt()])) if not Path(path).suffix else path)

    def _accept(self) -> None:
        text = self.path.text().strip()
        if not text:
            QMessageBox.warning(self, "Экспорт", "Укажите файл для сохранения.")
            return
        path = Path(text)
        if not path.suffix:
            path = path.with_suffix(EXTENSIONS[self._fmt()])
        if path.exists():
            if path.is_dir():
                QMessageBox.warning(self, "Экспорт", "Указана папка, а не файл.")
                return
            answer = QMessageBox.question(
                self, "Файл уже существует", f"Заменить существующий файл?\n{path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.params = {"path": str(path), "fmt": self._fmt(), "delimiter": self.delim.currentData(),
                       "formula_guard": self.guard.isChecked(), "full": self.r_full.isChecked()}
        self.accept()
