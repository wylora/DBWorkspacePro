"""Генератор документации базы данных."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from documentation.diagram import er_diagram_png
from documentation.generator import DocOptions
from documentation.service import DocumentationService
from reports.model import FORMAT_TITLES
from utils.tasks import DuplicateTaskError

from .automation_page import dir_row, profile_combo
from .context import AppContext
from .helpers import open_path
from .widgets import PageHeader

DOC_FORMATS = ["docx", "pdf", "html", "md"]


class DocumentationPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self.ds = DocumentationService(ctx.services)
        self._files: list[Path] = []
        self._png: bytes | None = None
        self._build()
        ctx.connections_changed.connect(self._reload_profiles)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Документация базы данных",
            "Таблицы, столбцы, ключи, индексы, связи, представления, процедуры, функции и триггеры — в Word, PDF, HTML "
            "или Markdown, со схемой связей. При повторном формировании показывается, что изменилось в структуре."))
        card = QFrame()
        card.setObjectName("Card")
        f = QFormLayout(card)
        self.c_profile = profile_combo(self.ctx)
        self.e_db = QLineEdit()
        self.e_title = QLineEdit()
        self.e_title.setPlaceholderText("по умолчанию: Документация базы данных «…»")
        self.e_org = QLineEdit()
        self.e_author = QLineEdit()
        self.k_defs = QCheckBox("Тексты представлений, процедур, функций и триггеров")
        self.k_defs.setChecked(True)
        self.k_diagram = QCheckBox("Схема связей (рисунок)")
        self.k_diagram.setChecked(True)
        self.k_idx = QCheckBox("Индексы и ограничения")
        self.k_idx.setChecked(True)
        self.k_changes = QCheckBox("Раздел «Изменения с прошлой версии документации»")
        self.k_changes.setChecked(True)
        fr = QHBoxLayout()
        self.k_fmt = {}
        for fm in DOC_FORMATS:
            cb = QCheckBox(FORMAT_TITLES[fm])
            cb.setChecked(fm == "docx")
            self.k_fmt[fm] = cb
            fr.addWidget(cb)
        fw = QWidget()
        fw.setLayout(fr)
        fr.setContentsMargins(0, 0, 0, 0)
        self.e_dir = QLineEdit(str(self.ds.output_dir()))
        for label, w in (("Подключение", self.c_profile), ("База данных", self.e_db), ("Заголовок", self.e_title),
                         ("Организация", self.e_org), ("Автор", self.e_author), ("", self.k_defs), ("", self.k_diagram),
                         ("", self.k_idx), ("", self.k_changes), ("Форматы", fw), ("Папка", dir_row(self.e_dir, self))):
            f.addRow(label, w)
        root.addWidget(card)
        row = QHBoxLayout()
        self.b_gen = QPushButton("Сформировать документацию")
        self.b_gen.setObjectName("Primary")
        self.b_gen.clicked.connect(self.generate)
        self.b_diag = QPushButton("Показать схему связей")
        self.b_diag.clicked.connect(self.preview_diagram)
        self.b_save_png = QPushButton("Сохранить схему PNG…")
        self.b_save_png.clicked.connect(self.save_png)
        self.b_save_png.setEnabled(False)
        for b in (self.b_gen, self.b_diag, self.b_save_png):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.files = QListWidget()
        self.files.setMaximumHeight(110)
        self.files.itemDoubleClicked.connect(lambda item: open_path(str(self._files[self.files.row(item)])))
        root.addWidget(self.files)
        self.diagram = QLabel("")
        self.diagram.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(self.diagram)
        root.addWidget(sa, 1)

    def _reload_profiles(self) -> None:
        cur = self.c_profile.currentData()
        self.c_profile.clear()
        for p in self.svc.profiles.repo.list():
            self.c_profile.addItem(p.name, p.id)
        self.c_profile.setCurrentIndex(max(0, self.c_profile.findData(cur)))

    def _target(self) -> tuple[str, str] | None:
        pid, db = self.c_profile.currentData(), self.e_db.text().strip()
        if not pid or not db:
            QMessageBox.information(self, "Документация", "Выберите подключение и укажите базу данных.")
            return None
        return pid, db

    def options(self) -> DocOptions:
        return DocOptions(self.e_title.text().strip(), self.e_org.text().strip(), self.e_author.text().strip(),
                          include_definitions=self.k_defs.isChecked(), include_diagram=self.k_diagram.isChecked(),
                          include_indexes=self.k_idx.isChecked(), include_changes=self.k_changes.isChecked())

    def generate(self) -> None:
        t = self._target()
        if t is None:
            return
        formats = [f for f, cb in self.k_fmt.items() if cb.isChecked()]
        if not formats:
            QMessageBox.information(self, "Документация", "Отметьте хотя бы один формат.")
            return
        opts, ds, out = self.options(), self.ds, Path(self.e_dir.text().strip() or self.ds.output_dir())

        def job(c):
            return ds.generate(t[0], t[1], opts, formats, output_dir=out, cancel_token=c.cancel_token,
                               progress=lambda s, f: c.report(s, f))

        try:
            self.ctx.runner.submit(f"Документация: {t[1]}", job, on_success=self._done,
                                   on_error=lambda m: QMessageBox.warning(self, "Документация", m), key="documentation")
        except DuplicateTaskError:
            return
        self.status.setText("Чтение структуры и формирование документации…")

    def _done(self, r) -> None:
        self._files = list(r.files)
        self.files.clear()
        for f in r.files:
            self.files.addItem(str(f))
        parts = [f"Готово: файлов {len(r.files)}."]
        if r.changes is not None:
            parts.append(f"Изменений структуры с прошлой версии: {r.changes}.")
        parts += [f"{FORMAT_TITLES.get(k, k)}: {v}" for k, v in r.errors.items()]
        if r.warnings:
            parts.append("Ограничения: " + "; ".join(r.warnings[:3]))
        self.status.setText(" ".join(parts))
        if r.model is not None:
            self._show_diagram(r.model)

    def preview_diagram(self) -> None:
        t = self._target()
        if t is None:
            return
        ds = self.ds
        try:
            self.ctx.runner.submit("Схема связей", lambda c: ds.read_model(t[0], t[1], cancel_token=c.cancel_token),
                                   on_success=self._show_diagram,
                                   on_error=lambda m: QMessageBox.warning(self, "Схема связей", m), key="er-diagram")
        except DuplicateTaskError:
            pass

    def _show_diagram(self, model) -> None:
        png, note = er_diagram_png(model)
        self._png = png
        self.b_save_png.setEnabled(png is not None)
        if png is None:
            self.diagram.setPixmap(QPixmap())
            self.diagram.setText(note)
            return
        pix = QPixmap()
        pix.loadFromData(png, "PNG")
        self.diagram.setPixmap(pix)
        self.status.setText((self.status.text() + " " if self.status.text() else "") + note)

    def save_png(self) -> None:
        if not self._png:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить схему", f"Схема_{self.e_db.text().strip()}.png", "PNG (*.png)")
        if path:
            Path(path).write_bytes(self._png)
