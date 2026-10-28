"""Генератор отчётов: подключение, шаблон, параметры, форматы, вывод, выполнение в фоне."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from database.connection import ConnectionState
from database.errors import DbWorkspaceError
from database.metadata import MetadataReader
from reports.generator import GenerationResult, ReportCancelled, ReportError, family_exists
from reports.model import BLOCK_TITLES, FORMAT_TITLES, FORMATS, ReportTemplate, new_id, validate_template
from reports.quick import FAV_PREFIX, appendix_blocks, template_from_query
from reports.styles import STYLES
from reports.templates import TemplateError
from utils.tasks import DuplicateTaskError, TaskCancelled

from .context import AppContext
from .helpers import open_path
from .widgets import EmptyState, PageHeader

log = logging.getLogger("dbworkspace.reports")
POLICIES = [("Не затирать: добавить номер к имени", "new_version"), ("Перезаписывать существующие файлы", "overwrite"),
            ("Пропускать, если файл уже есть", "skip")]
PARAM_HINTS = {"int": "целое число", "decimal": "число", "date": "ГГГГ-ММ-ДД или ДД.ММ.ГГГГ", "bool": "да / нет", "text": ""}


class ReportsPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self.rs = ctx.reports
        self.template: ReportTemplate | None = None
        self._param_edits: dict[str, QLineEdit] = {}
        self._task = None
        self._partial: GenerationResult | None = None
        self._db_cache: dict[str, list[str]] = {}
        self._building = False
        self._build()
        ctx.connections_changed.connect(self._reload_connections)
        ctx.reports_changed.connect(self._reload_templates)
        ctx.settings_changed.connect(self._update_pdf)
        ctx.runner.task_changed.connect(self._on_task)
        self._reload_connections()
        self._reload_templates()

    # ================================================================ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Отчёты Word и другие форматы",
            "Выберите подключение и шаблон, заполните параметры и сформируйте отчёт. Данные только читаются; "
            "результат — файлы в выбранных форматах."))
        self.no_conn = EmptyState("Нет активных подключений",
                                  "Подключитесь к SQL Server на странице «Подключения» — тогда можно будет формировать отчёты.",
                                  "Перейти к подключениям")
        self.no_conn.action_clicked.connect(lambda: self.ctx.navigate.emit("connections"))
        root.addWidget(self.no_conn)

        self.main = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.main, 1)

        # ---- левая часть: настройки
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        src = QFormLayout()
        self.c_conn = QComboBox()
        self.c_conn.activated.connect(self._on_conn)
        self.c_db = QComboBox()
        self.c_db.setEditable(True)
        self.c_db.setToolTip("База данных для запросов шаблона. Пусто — база по умолчанию для логина.")
        self.c_tpl = QComboBox()
        self.c_tpl.activated.connect(self._on_template)
        b_edit = QPushButton("Изменить в конструкторе")
        b_edit.clicked.connect(self._edit_template)
        src.addRow("Подключение", self.c_conn)
        src.addRow("База данных", self.c_db)
        src.addRow("Шаблон", self.c_tpl)
        src.addRow("", b_edit)
        cl.addLayout(src)

        self.grp_params = QGroupBox("Параметры отчёта")
        self.param_form = QFormLayout(self.grp_params)
        cl.addWidget(self.grp_params)

        look = QGroupBox("Оформление (переопределяет шаблон, необязательно)")
        lf = QFormLayout(look)
        self.e_title = QLineEdit()
        self.e_title.setPlaceholderText("как в шаблоне")
        self.e_org = QLineEdit()
        self.e_author = QLineEdit()
        self.c_style = QComboBox()
        self.c_style.addItem("(как в шаблоне)", "")
        for s in STYLES.values():
            self.c_style.addItem(s.title, s.id)
        self.c_style.activated.connect(lambda _i: self._refresh_structure())
        lf.addRow("Название отчёта", self.e_title)
        lf.addRow("Организация", self.e_org)
        lf.addRow("Автор", self.e_author)
        lf.addRow("Стиль", self.c_style)
        self.k_code = QCheckBox("Добавить в конец отчёта код запросов и их результаты")
        self.k_code.setToolTip("Для каждого источника данных: текст запроса, значения параметров, число строк, время "
                               "выполнения и таблица результата.")
        self.k_code.toggled.connect(lambda _on: self._refresh_structure())
        lf.addRow("", self.k_code)
        cl.addWidget(look)

        out = QGroupBox("Результат")
        of = QFormLayout(out)
        self.k_fmt: dict[str, QCheckBox] = {}
        saved = self.rs.saved_formats()
        for f in FORMATS:
            cb = QCheckBox(FORMAT_TITLES[f])
            cb.setChecked(f in saved)
            self.k_fmt[f] = cb
        self._lo = None
        self._update_pdf()
        for i in range(0, len(FORMATS), 3):
            row = QHBoxLayout()
            for f in FORMATS[i:i + 3]:
                row.addWidget(self.k_fmt[f])
            of.addRow("Форматы" if i == 0 else "", self._hbox(row))
        self.e_dir = QLineEdit(str(self.rs.output_dir()))
        b_dir = QPushButton("Обзор…")
        b_dir.clicked.connect(self._pick_dir)
        drow = QHBoxLayout()
        drow.addWidget(self.e_dir, 1)
        drow.addWidget(b_dir)
        self.e_name = QLineEdit()
        self.e_name.setPlaceholderText("пусто — «Название_дата_время»")
        self.c_policy = QComboBox()
        for text, val in POLICIES:
            self.c_policy.addItem(text, val)
        self.c_policy.setCurrentIndex(max(0, self.c_policy.findData(self.svc.settings.get("report_overwrite"))))
        self.s_rows = QSpinBox()
        self.s_rows.setRange(1, 100000)
        self.s_rows.setValue(self.svc.settings.get("report_max_rows"))
        self.s_rows.setSuffix(" строк")
        self.s_rows.setToolTip("Максимум строк на источник и на таблицу. Для очень больших выгрузок используйте "
                               "экспорт из SQL-редактора (потоком в CSV/Excel).")
        self.s_part = QSpinBox()
        self.s_part.setRange(0, 100000)
        self.s_part.setSpecialValueText("как в шаблоне")
        self.s_part.setSuffix(" строк")
        self.s_part.setToolTip("Если таблица длиннее — отчёт делится на части (отдельные файлы).")
        of.addRow("Папка", self._hbox(drow))
        of.addRow("Имя файла", self.e_name)
        of.addRow("Если файл есть", self.c_policy)
        of.addRow("Лимит строк", self.s_rows)
        of.addRow("Часть отчёта", self.s_part)
        cl.addWidget(out)

        btns = QHBoxLayout()
        self.b_run = QPushButton("Сформировать отчёт")
        self.b_run.setObjectName("Primary")
        self.b_run.clicked.connect(self.run)
        self.b_cancel = QPushButton("Отмена")
        self.b_cancel.setEnabled(False)
        self.b_cancel.clicked.connect(self.cancel)
        btns.addWidget(self.b_run)
        btns.addWidget(self.b_cancel)
        btns.addStretch(1)
        cl.addLayout(btns)
        cl.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(card)
        sa.setMinimumWidth(430)
        self.main.addWidget(sa)

        # ---- правая часть: структура и результат
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self._title("Структура отчёта (предпросмотр)"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        rl.addWidget(self.tree, 3)
        rl.addWidget(self._title("Ход выполнения и результат"))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.status = QLabel("Готово к формированию.")
        self.status.setWordWrap(True)
        self.status.setObjectName("Muted")
        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(lambda _i: self._open_file())
        frow = QHBoxLayout()
        for text, fn in (("Открыть файл", self._open_file), ("Открыть папку", self._open_folder)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            frow.addWidget(b)
        frow.addStretch(1)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(130)
        self.notes.setPlaceholderText("Предупреждения и ошибки формирования появятся здесь")
        for w in (self.progress, self.status, self.files):
            rl.addWidget(w)
        rl.addLayout(frow)
        rl.addWidget(self.notes)
        self.main.addWidget(right)
        self.main.setSizes([520, 560])

    @staticmethod
    def _hbox(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return w

    @staticmethod
    def _title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("CardTitle")
        return lbl

    # ======================================================= данные списков
    def _reload_connections(self) -> None:
        cur = self.c_conn.currentData()
        self.c_conn.blockSignals(True)
        self.c_conn.clear()
        for p in self.svc.profiles.repo.list():
            conn = self.svc.manager.primary(p.id)
            if conn is not None and conn.state in (ConnectionState.CONNECTED, ConnectionState.LOST):
                self.c_conn.addItem(p.name, p.id)
        self.c_conn.setCurrentIndex(max(0, self.c_conn.findData(cur)))
        self.c_conn.blockSignals(False)
        has = self.c_conn.count() > 0
        self.no_conn.setVisible(not has)
        self.main.setVisible(has)
        active = {self.c_conn.itemData(i) for i in range(self.c_conn.count())}
        for pid in [k for k in self._db_cache if k not in active]:
            del self._db_cache[pid]
        if has:
            self._load_databases()
        self._update_buttons()

    def _on_conn(self, _i) -> None:
        self.c_db.setEditText("")
        self._load_databases()

    def _load_databases(self) -> None:
        pid = self.c_conn.currentData()
        conn = self.svc.manager.primary(pid) if pid else None
        cur = self.c_db.currentText()
        self.c_db.clear()
        self.c_db.addItem("")
        if conn is None:
            return

        def fill(names):
            self._db_cache[pid] = names
            if self.c_conn.currentData() != pid:
                return
            keep = self.c_db.currentText()
            self.c_db.clear()
            self.c_db.addItem("")
            for n in names:
                self.c_db.addItem(n)
            self.c_db.setEditText(keep or conn.profile.database)

        if pid in self._db_cache:
            fill(self._db_cache[pid])
            self.c_db.setEditText(cur or conn.profile.database)
            return
        batch = self.svc.settings.get("fetch_batch_size")
        try:
            self.ctx.runner.submit("Отчёты: список баз", lambda c: MetadataReader(conn, batch_size=batch).list_databases(),
                                   on_success=fill, on_error=lambda m: self.ctx.message.emit(m), key=f"reports-dbs:{pid}")
        except DuplicateTaskError:
            pass

    def _update_pdf(self) -> None:
        """LibreOffice ищется заново: после установки или смены пути в настройках перезапуск не нужен."""
        self._lo = self.rs.soffice()
        cb = self.k_fmt["pdf"]
        cb.setEnabled(bool(self._lo))
        if self._lo:
            cb.setToolTip(f"PDF формирует LibreOffice: {self._lo}")
        else:
            cb.setChecked(False)
            cb.setToolTip("Для PDF нужен LibreOffice: установите его с libreoffice.org или укажите файл soffice.exe "
                          "либо папку LibreOffice в «Настройках».")

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._update_pdf()

    def _reload_templates(self) -> None:
        cur = self.c_tpl.currentData()
        infos = self.rs.templates.list()
        self.c_tpl.blockSignals(True)
        self.c_tpl.clear()
        for i in infos:
            self.c_tpl.addItem(i.name + ("  (встроенный)" if i.builtin else ""), i.id)
        favorites = self.svc.favorites.list()
        if favorites:
            self.c_tpl.insertSeparator(self.c_tpl.count())
            for f in favorites:
                self.c_tpl.addItem(f"Сохранённый запрос: {f.title}", f"{FAV_PREFIX}{f.id}")
        self.c_tpl.setCurrentIndex(max(0, self.c_tpl.findData(cur)))
        self.c_tpl.blockSignals(False)
        self._on_template(0)

    def _on_template(self, _i) -> None:
        tid = self.c_tpl.currentData()
        self.template = None
        if tid and str(tid).startswith(FAV_PREFIX):
            fav = next((f for f in self.svc.favorites.list() if f"{FAV_PREFIX}{f.id}" == tid), None)
            if fav is not None:
                self.template = template_from_query(fav.title, fav.sql, key=str(fav.id))
        elif tid:
            try:
                self.template = self.rs.templates.load(tid)
            except TemplateError as exc:
                self.ctx.message.emit(str(exc))
        self._rebuild_params()
        self._refresh_structure()
        self._update_buttons()

    def _edit_template(self) -> None:
        if self.template is not None and self.template.id.startswith(FAV_PREFIX):
            # отчёт по сохранённому запросу — сохраняем как обычный шаблон, чтобы его можно было настроить
            t = ReportTemplate.from_dict(self.template.to_dict())
            t.id = new_id("t")
            self.rs.templates.save(t)
            self.ctx.message.emit(f"Создан шаблон «{t.name}» на основе сохранённого запроса")
            self.ctx.reports_changed.emit()
            self.ctx.open_template.emit(t.id)
        elif self.template is not None:
            self.ctx.open_template.emit(self.template.id)
        self.ctx.navigate.emit("templates")

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для отчёта", self.e_dir.text())
        if d:
            self.e_dir.setText(d)

    # ===================================================== параметры/структура
    def _rebuild_params(self) -> None:
        while self.param_form.rowCount() > 0:
            self.param_form.removeRow(0)
        self._param_edits = {}
        t = self.template
        self.grp_params.setVisible(bool(t and t.params))
        if not t:
            return
        for p in t.params:
            e = QLineEdit(p.default)
            hint = PARAM_HINTS.get(p.type, "")
            e.setPlaceholderText(hint)
            self._param_edits[p.name] = e
            self.param_form.addRow(f"{p.label or p.name}{' *' if p.required else ''}", e)

    def _refresh_structure(self) -> None:
        self.tree.clear()
        if self.template is None:
            return
        t = self._prepared_template()                # с учётом приложения «Код запросов и результаты»
        style = self.c_style.currentData() or t.style
        root = QTreeWidgetItem(self.tree, [f"{t.name}  ·  стиль «{STYLES[style].title}»" if style in STYLES else t.name])
        if t.title_page:
            QTreeWidgetItem(root, ["Титульная страница"])
        if t.toc:
            QTreeWidgetItem(root, ["Оглавление"])
        for b in t.blocks:
            label = BLOCK_TITLES.get(b.type, b.type) + (f": {b.title}" if b.title else "")
            it = QTreeWidgetItem(root, [label])
            src = t.source(b.source) if b.source else None
            if src is not None:
                QTreeWidgetItem(it, [f"данные: {src.name or src.id}"])
            if b.type == "table":
                bits = []
                if b.columns:
                    bits.append(f"столбцов: {len([c for c in b.columns if not c.hidden])}")
                if b.sort:
                    bits.append("сортировка")
                if b.group_by:
                    bits.append(f"разделы по «{b.group_by}»")
                if any(c.total != "none" for c in b.columns):
                    bits.append("итоги")
                if bits:
                    QTreeWidgetItem(it, [", ".join(bits)])
            elif b.type == "chart":
                QTreeWidgetItem(it, [f"{b.chart_kind}: {b.x} → {', '.join(b.y) or 'количество'}"])
        if t.sources:
            srcs = QTreeWidgetItem(root, ["Источники данных"])
            for s in t.sources:
                QTreeWidgetItem(srcs, [f"{s.name or s.id}  [{ {'sql': 'SQL', 'table': 'таблица', 'procedure': 'процедура'}.get(s.kind, s.kind) }]"])
        if t.params:
            ps = QTreeWidgetItem(root, ["Параметры"])
            for p in t.params:
                QTreeWidgetItem(ps, [f"{p.label or p.name} ({p.type}){' *' if p.required else ''}"])
        QTreeWidgetItem(root, ["Часть большого отчёта: " + (f"по {t.part_rows} строк" if t.part_rows else "одним файлом")])
        self.tree.expandAll()

    def _update_buttons(self) -> None:
        busy = self._task is not None and self._task.is_active
        self.b_run.setEnabled(not busy and self.c_conn.count() > 0 and self.template is not None)
        self.b_cancel.setEnabled(busy)

    # ============================================================ запуск
    def _selected_formats(self) -> list[str]:
        return [f for f in FORMATS if self.k_fmt[f].isChecked() and self.k_fmt[f].isEnabled()]

    def _prepared_template(self) -> ReportTemplate:
        t = ReportTemplate.from_dict(self.template.to_dict())
        if self.e_title.text().strip():
            t.title = self.e_title.text().strip()
        if self.e_org.text().strip():
            t.organization = self.e_org.text().strip()
        if self.e_author.text().strip():
            t.author = self.e_author.text().strip()
        if self.c_style.currentData():
            t.style = self.c_style.currentData()
        if self.s_part.value() > 0:
            t.part_rows = self.s_part.value()
        if self.k_code.isChecked() and not t.id.startswith(FAV_PREFIX):
            t.blocks += appendix_blocks(t)
        return t

    def run(self) -> None:
        if self.template is None or (self._task is not None and self._task.is_active):
            return
        pid = self.c_conn.currentData()
        formats = self._selected_formats()
        if not pid:
            QMessageBox.information(self, "Отчёт", "Выберите активное подключение.")
            return
        if not formats:
            QMessageBox.information(self, "Отчёт", "Отметьте хотя бы один формат результата.")
            return
        out_dir = self.e_dir.text().strip()
        if not out_dir:
            QMessageBox.information(self, "Отчёт", "Укажите папку для результата.")
            return
        t = self._prepared_template()
        errs = validate_template(t)
        if errs:
            QMessageBox.warning(self, "Шаблон содержит ошибки",
                                "Исправьте шаблон в конструкторе:\n\n• " + "\n• ".join(errs[:10]))
            return
        policy = self.c_policy.currentData()
        base = self.e_name.text().strip()
        if policy == "overwrite" and base:
            existing = [f for f in formats if family_exists(Path(out_dir), base, f)]
            if existing:
                a = QMessageBox.warning(
                    self, "Перезапись файлов", "В папке уже есть файлы с таким именем (" + ", ".join(existing) +
                    "). Они будут заменены. Продолжить?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if a != QMessageBox.StandardButton.Yes:
                    return
        raw = {name: e.text() for name, e in self._param_edits.items()}
        try:
            req = self.rs.build_request(t, raw, formats, pid, self.c_db.currentText().strip(), output_dir=out_dir,
                                        base_name=base, overwrite=policy, max_rows=self.s_rows.value())
        except ReportError as exc:
            QMessageBox.warning(self, "Отчёт", str(exc))
            return
        self.rs.remember_formats(formats)
        self.files.clear()
        self.notes.clear()
        self._partial = None
        rs = self.rs

        def job(c):
            def progress(stage, frac):
                c.report(stage, frac)
            try:
                return rs.run(req, progress=progress, cancel_token=c.cancel_token)
            except ReportCancelled as exc:
                self._partial = exc.result
                raise TaskCancelled() from exc

        try:
            self._task = self.ctx.runner.submit(f"Отчёт: {t.name}", job, on_success=self._done, on_error=self._failed,
                                                on_cancelled=self._cancelled, key="report-generate")
        except DuplicateTaskError:
            return
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText("Формирование отчёта…")
        self._update_buttons()

    def cancel(self) -> None:
        if self._task is not None and self._task.is_active:
            self.svc.tasks.cancel(self._task.id)
            self.status.setText("Отмена… (уже готовые файлы сохраняются)")

    def _on_task(self, task) -> None:
        if self._task is None or task.id != self._task.id:
            return
        if task.is_active:
            if task.progress is None:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(int(task.progress * 100))
            if task.stage:
                self.status.setText(task.stage)

    # ---- завершение
    def _finish_ui(self) -> None:
        self.progress.hide()
        self._task = None
        self._update_buttons()

    def _show_files(self, res: GenerationResult) -> None:
        self.files.clear()
        for f in res.files:
            self.files.addItem(f"{FORMAT_TITLES.get(f.format, f.format)}  —  {f.path}")
            self.files.item(self.files.count() - 1).setData(Qt.ItemDataRole.UserRole, str(f.path))
        lines = []
        lines += [f"Не создан {FORMAT_TITLES.get(k, k)}: {v}" for k, v in res.errors.items()]
        lines += res.skipped
        lines += list(dict.fromkeys(res.warnings))
        self.notes.setPlainText("\n".join(lines))

    def _done(self, res: GenerationResult) -> None:
        self._finish_ui()
        self._show_files(res)
        if res.ok and not res.errors:
            self.status.setText(f"Готово за {res.elapsed:.1f} с. Файлов: {len(res.files)}"
                                + (f" (частей отчёта: {res.parts})" if res.parts > 1 else "") + ".")
        elif res.ok:
            self.status.setText("Отчёт сформирован частично: часть форматов не создана (см. ниже).")
            QMessageBox.warning(self, "Отчёт сформирован частично", "\n".join(
                f"{FORMAT_TITLES.get(k, k)}: {v}" for k, v in res.errors.items()))
        else:
            self.status.setText("Файлы не созданы." if not res.skipped else "Все файлы уже существуют — пропущено.")
        self.ctx.reports_changed.emit()

    def _failed(self, msg: str) -> None:
        self._finish_ui()
        self.status.setText("Ошибка формирования отчёта.")
        self.notes.setPlainText(msg)
        QMessageBox.critical(self, "Не удалось сформировать отчёт", msg)
        self.ctx.reports_changed.emit()

    def _cancelled(self) -> None:
        self._finish_ui()
        res = self._partial
        if res is not None and res.files:
            self._show_files(res)
            self.status.setText("Формирование отменено. Уже готовые файлы сохранены.")
        else:
            self.status.setText("Формирование отменено.")
        self.ctx.reports_changed.emit()

    # ---- открытие
    def _selected_path(self) -> str | None:
        item = self.files.currentItem() or (self.files.item(0) if self.files.count() else None)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _open_file(self) -> None:
        p = self._selected_path()
        if p:
            open_path(p)

    def _open_folder(self) -> None:
        p = self._selected_path()
        open_path(str(Path(p).parent) if p else self.e_dir.text())
