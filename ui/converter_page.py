"""Конвертер файлов: перетаскивание, очередь, выбор формата, пакетная обработка в фоне, история."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from converter.engine import ConvertOptions, Job, JobResult, expand_paths
from converter.formats import (FORMATS, PRIMARY_EXT, SOURCE_FORMATS, TARGET_FORMATS, Capabilities, detect_format, plan,
                               targets_for)
from converter.libreoffice import INSTALL_HINT
from converter.service import STATUS_TEXT
from utils.tasks import DuplicateTaskError

from .context import AppContext
from .helpers import open_path
from .theme import colors
from .widgets import PageHeader

log = logging.getLogger("dbworkspace.converter")
POLICIES = [("Не затирать: добавить номер к имени", "new_version"), ("Перезаписывать существующие файлы", "overwrite"),
            ("Пропускать, если файл уже есть", "skip")]
COLS = ["Файл", "Формат", "Размер", "Состояние", "Результат / сообщение"]


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class DropZone(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        self.setMinimumHeight(90)
        lay = QVBoxLayout(self)
        self.label = QLabel("Перетащите сюда файлы или папки для преобразования")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setObjectName("Muted")
        lay.addWidget(self.label)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


class ConvSignals(QObject):
    row_done = Signal(int, object)


class CompatDialog(QDialog):
    """Таблица совместимости: что во что можно преобразовать и каким способом."""

    def __init__(self, parent, caps: Capabilities):
        super().__init__(parent)
        self.setWindowTitle("Таблица совместимости форматов")
        self.resize(1000, 520)
        lay = QVBoxLayout(self)
        legend = QLabel("Строки — исходный формат, столбцы — результат.   ✓ — встроенный преобразователь;   "
                        "LO — LibreOffice;   2 шага — через промежуточный формат;   — — не поддерживается;   "
                        "серым — нужен LibreOffice, которого нет.\nНаведите курсор на ячейку, чтобы увидеть, что при "
                        "этом сохраняется, а что теряется.")
        legend.setWordWrap(True)
        lay.addWidget(legend)
        table = QTableWidget(len(SOURCE_FORMATS), len(TARGET_FORMATS))
        table.setHorizontalHeaderLabels([PRIMARY_EXT[f] for f in TARGET_FORMATS])
        table.setVerticalHeaderLabels([PRIMARY_EXT[f] for f in SOURCE_FORMATS])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, s in enumerate(SOURCE_FORMATS):
            for c, d in enumerate(TARGET_FORMATS):
                route = plan(s, d)
                if s == d:
                    text, tip = "·", "Тот же формат"
                elif route is None:
                    text, tip = "—", "Не поддерживается"
                else:
                    text = {"python": "✓", "libreoffice": "LO", "chain": "2 шага", "copy": "✓"}[route.kind]
                    tip = f"{route.description}\n" + "\n".join("• " + n for n in route.notes)
                    if route.needs_lo and not caps.has_lo:
                        tip += "\n\nНужен LibreOffice (не найден)."
                item = QTableWidgetItem(text)
                item.setToolTip(tip.strip())
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(r, c, item)
        table.resizeColumnsToContents()
        lay.addWidget(table, 1)
        status = QLabel("LibreOffice найден." if caps.has_lo else INSTALL_HINT)
        status.setWordWrap(True)
        lay.addWidget(status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


class ConverterPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self.cs = ctx.converter
        self.jobs: list[Job] = []
        self.results: dict[int, JobResult] = {}
        self._task = None
        self._summary = None
        self.signals = ConvSignals(self)
        self.signals.row_done.connect(self._on_row_done)
        self._build()
        ctx.runner.task_changed.connect(self._on_task)
        self._refresh_all()

    # ============================================================== UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Конвертер файлов",
            "Документы (DOCX, DOC, ODT, RTF, HTML, Markdown, TXT → PDF и другие), таблицы (XLSX, XLS, CSV, TSV, JSON) и "
            "SQL-скрипты. Исходные файлы не изменяются. Не все пары форматов возможны — см. «Таблицу совместимости»."))
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        main = QWidget()
        ml = QVBoxLayout(main)
        self.drop = DropZone()
        self.drop.files_dropped.connect(self._add_paths)
        ml.addWidget(self.drop)
        row = QHBoxLayout()
        for text, fn in (("Добавить файлы…", self._pick_files), ("Добавить папку…", self._pick_folder)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        self.k_recursive = QCheckBox("Включая вложенные папки")
        self.k_recursive.setChecked(True)
        row.addWidget(self.k_recursive)
        row.addStretch(1)
        self.b_remove = QPushButton("Убрать выбранные")
        self.b_remove.clicked.connect(self._remove_selected)
        self.b_clear = QPushButton("Очистить список")
        self.b_clear.clicked.connect(self._clear)
        row.addWidget(self.b_remove)
        row.addWidget(self.b_clear)
        ml.addLayout(row)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setColumnWidth(0, 260)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._open_result(r))
        ml.addWidget(self.table, 1)

        opt = QFrame()
        opt.setObjectName("Card")
        of = QFormLayout(opt)
        of.setContentsMargins(14, 10, 14, 10)
        self.c_target = QComboBox()
        self.c_target.activated.connect(lambda _i: self._refresh_notes())
        self.lbl_blocked = QLabel("")
        self.lbl_blocked.setWordWrap(True)
        self.lbl_blocked.setObjectName("Muted")
        self.k_same = QCheckBox("Сохранять рядом с исходным файлом")
        self.k_same.setChecked(self.svc.settings.get("convert_same_dir"))
        self.k_same.toggled.connect(self._on_same)
        self.e_dir = QLineEdit(str(self.cs.options().out_dir))
        b_dir = QPushButton("Обзор…")
        b_dir.clicked.connect(self._pick_dir)
        drow = QHBoxLayout()
        drow.addWidget(self.e_dir, 1)
        drow.addWidget(b_dir)
        dwrap = QWidget()
        dwrap.setLayout(drow)
        drow.setContentsMargins(0, 0, 0, 0)
        self.c_policy = QComboBox()
        for text, val in POLICIES:
            self.c_policy.addItem(text, val)
        self.c_policy.setCurrentIndex(max(0, self.c_policy.findData(self.svc.settings.get("convert_policy"))))
        of.addRow("Преобразовать в", self.c_target)
        of.addRow("", self.lbl_blocked)
        of.addRow("", self.k_same)
        of.addRow("Папка результата", dwrap)
        of.addRow("Если файл уже есть", self.c_policy)
        ml.addWidget(opt)

        run = QHBoxLayout()
        self.b_run = QPushButton("Преобразовать")
        self.b_run.setObjectName("Primary")
        self.b_run.clicked.connect(self.run)
        self.b_cancel = QPushButton("Отмена")
        self.b_cancel.clicked.connect(self.cancel)
        b_compat = QPushButton("Таблица совместимости…")
        b_compat.clicked.connect(self._show_compat)
        self.b_open = QPushButton("Открыть папку результата")
        self.b_open.clicked.connect(self._open_folder)
        for w in (self.b_run, self.b_cancel, b_compat, self.b_open):
            run.addWidget(w)
        run.addStretch(1)
        ml.addLayout(run)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.status = QLabel("Добавьте файлы, выберите формат результата и нажмите «Преобразовать».")
        self.status.setWordWrap(True)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(110)
        self.notes.setPlaceholderText("Здесь появятся замечания к выбранному преобразованию и результаты проверки")
        for w in (self.progress, self.status, self.notes):
            ml.addWidget(w)
        self.tabs.addTab(main, "Преобразование")

        hist = QWidget()
        hl = QVBoxLayout(hist)
        self.hist = QTableWidget(0, 4)
        self.hist.setHorizontalHeaderLabels(["Время", "Операция", "Статус", "Результат / сообщение"])
        self.hist.horizontalHeader().setStretchLastSection(True)
        self.hist.verticalHeader().setVisible(False)
        self.hist.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hist.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.hist.cellDoubleClicked.connect(self._open_history)
        hl.addWidget(QLabel("Последние операции (двойной щелчок — открыть результат)."))
        hl.addWidget(self.hist, 1)
        self._history = []
        self.tabs.addTab(hist, "История")

    # ======================================================== список файлов
    def _pick_files(self) -> None:
        exts = " ".join("*" + e for f in SOURCE_FORMATS for e in FORMATS[f].exts)
        paths, _ = QFileDialog.getOpenFileNames(self, "Файлы для преобразования", "",
                                                f"Поддерживаемые форматы ({exts});;Все файлы (*)")
        if paths:
            self._add_paths(paths)

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка с файлами")
        if d:
            self._add_paths([d])

    def _add_paths(self, paths) -> None:
        if self._busy():
            return
        known = {j.src.resolve() for j in self.jobs}
        new, ignored, truncated = expand_paths([Path(p) for p in paths], self.k_recursive.isChecked())
        added = 0
        for j in new:
            try:
                key = j.src.resolve()
            except OSError:
                continue
            if key not in known:
                known.add(key)
                self.jobs.append(j)
                added += 1
        msg = f"Добавлено файлов: {added}."
        if ignored:
            msg += f" Пропущено (формат не поддерживается или файл не найден): {len(ignored)}."
        if truncated:
            msg += " Достигнут предел числа файлов — добавлена только часть."
        self.status.setText(msg)
        self._render_table()
        self._refresh_all()

    def _remove_selected(self) -> None:
        if self._busy():
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.jobs):
                del self.jobs[r]
        self.results.clear()
        self._render_table()
        self._refresh_all()

    def _clear(self) -> None:
        if self._busy():
            return
        self.jobs.clear()
        self.results.clear()
        self._render_table()
        self._refresh_all()

    def _render_table(self) -> None:
        self.table.setRowCount(len(self.jobs))
        for r, j in enumerate(self.jobs):
            self._set_row(r, j, self.results.get(r))

    def _set_row(self, r: int, j: Job, res: JobResult | None) -> None:
        fmt = detect_format(j.src)
        try:
            size = human_size(j.src.stat().st_size)
        except OSError:
            size = "—"
        name = str(j.rel_dir / j.src.name) if str(j.rel_dir) != "." else j.src.name
        state = STATUS_TEXT[res.status] if res else "Ожидает"
        info = ""
        if res:
            info = res.error or (", ".join(p.name for p in res.outputs[:3]) + (" …" if len(res.outputs) > 3 else ""))
        cells = [name, FORMATS[fmt].title if fmt else "?", size, state, info]
        c = colors(self.ctx.theme)
        color = {"ok": c["success"], "failed": c["danger"], "skipped": c["warning"], "unsupported": c["warning"],
                 "cancelled": c["warning"]}.get(res.status) if res else None
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if res and col >= 3:
                item.setToolTip("\n".join(([res.error] if res.error else []) + res.warnings) or text)
            self.table.setItem(r, col, item)
        if color:
            from PySide6.QtGui import QBrush, QColor
            self.table.item(r, 3).setForeground(QBrush(QColor(color)))

    # ===================================================== форматы и замечания
    def _caps(self) -> Capabilities:
        return self.cs.caps()

    def _refresh_all(self) -> None:
        self._refresh_targets()
        self._refresh_notes()
        self._update_buttons()
        self._refresh_history()

    def _refresh_targets(self) -> None:
        caps = self._caps()
        counts: dict[str, int] = {}
        blocked: set[str] = set()
        usable = 0
        for j in self.jobs:
            fmt = detect_format(j.src)
            if fmt not in SOURCE_FORMATS:
                continue
            usable += 1
            for dst, route, ok, _why in targets_for(fmt, caps):
                if route is None:
                    continue
                if ok:
                    counts[dst] = counts.get(dst, 0) + 1
                else:
                    blocked.add(dst)
        cur = self.c_target.currentData() or self.svc.settings.get("convert_last_format")
        self.c_target.clear()
        for dst in TARGET_FORMATS:
            if dst in counts:
                self.c_target.addItem(f"{FORMATS[dst].title} — для {counts[dst]} из {len(self.jobs)} файл(ов)", dst)
        self.c_target.setCurrentIndex(max(0, self.c_target.findData(cur)))
        miss = sorted(b for b in blocked if b not in counts)
        self.lbl_blocked.setText("Без LibreOffice недоступны: " + ", ".join(FORMATS[b].title for b in miss) +
                                 ". " + INSTALL_HINT if miss else "")

    def _refresh_notes(self) -> None:
        dst = self.c_target.currentData()
        if not dst or not self.jobs:
            self.notes.setPlainText("")
            return
        lines, seen = [], set()
        for j in self.jobs:
            fmt = detect_format(j.src)
            route = plan(fmt, dst) if fmt else None
            if route is None or (fmt, dst) in seen:
                continue
            seen.add((fmt, dst))
            lines.append(f"{FORMATS[fmt].title} → {FORMATS[dst].title}: {route.description}")
            lines += ["    • " + n for n in route.notes]
        unsupported = sum(1 for j in self.jobs if not (detect_format(j.src) and plan(detect_format(j.src), dst)))
        if unsupported:
            lines.append(f"Файлов, которые нельзя преобразовать в этот формат: {unsupported} (они будут пропущены).")
        self.notes.setPlainText("\n".join(lines))

    def _show_compat(self) -> None:
        CompatDialog(self, self._caps()).exec()

    # ========================================================== вывод/запуск
    def _on_same(self, on: bool) -> None:
        self.e_dir.setEnabled(not on)

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка результата", self.e_dir.text())
        if d:
            self.e_dir.setText(d)

    def _busy(self) -> bool:
        return self._task is not None and self._task.is_active

    def _update_buttons(self) -> None:
        busy = self._busy()
        self.b_run.setEnabled(not busy and bool(self.jobs) and self.c_target.count() > 0)
        self.b_cancel.setEnabled(busy)
        for b in (self.b_remove, self.b_clear):
            b.setEnabled(not busy)

    def run(self) -> None:
        if self._busy() or not self.jobs:
            return
        dst = self.c_target.currentData()
        if not dst:
            QMessageBox.information(self, "Конвертер", "Нет формата, подходящего для выбранных файлов.")
            return
        same = self.k_same.isChecked()
        out = self.e_dir.text().strip()
        if not same and not out:
            QMessageBox.information(self, "Конвертер", "Укажите папку результата.")
            return
        policy = self.c_policy.currentData()
        if policy == "overwrite":
            a = QMessageBox.warning(self, "Перезапись файлов",
                                    "Существующие файлы результата будут заменены. Исходные файлы не изменяются.\n\nПродолжить?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No)
            if a != QMessageBox.StandardButton.Yes:
                return
        self.svc.settings.set("convert_last_format", dst)
        opts = self.cs.options(out_dir=Path(out) if out else None, same_dir=same, policy=policy)
        jobs = list(self.jobs)
        self.results.clear()
        self._render_table()
        cs, signals = self.cs, self.signals

        def job(c):
            return cs.run(jobs, dst, opts, progress=lambda s, f: c.report(s, f), cancel_token=c.cancel_token,
                          on_result=lambda i, r: signals.row_done.emit(i, r))

        try:
            self._task = self.ctx.runner.submit(f"Конвертация: {len(jobs)} файл(ов) → {FORMATS[dst].title}", job,
                                                on_success=self._done, on_error=self._failed, key="convert-batch")
        except DuplicateTaskError:
            return
        self._dst = dst
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText("Преобразование…")
        self._update_buttons()

    def cancel(self) -> None:
        if self._busy():
            self.svc.tasks.cancel(self._task.id)
            self.status.setText("Отмена… (текущий файл завершится или будет прерван; готовые результаты сохраняются)")

    def _on_task(self, task) -> None:
        if self._task is None or task.id != self._task.id or not task.is_active:
            return
        if task.progress is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(task.progress * 100))
        if task.stage:
            self.status.setText(task.stage)

    def _on_row_done(self, i: int, res) -> None:
        self.results[i] = res
        if 0 <= i < len(self.jobs):
            self._set_row(i, self.jobs[i], res)

    def _done(self, summary) -> None:
        self._summary = summary
        self.progress.hide()
        self._task = None
        for i, r in enumerate(summary.results):
            self.results[i] = r
        self._render_table()
        problems = summary.check_consistency()
        text = (f"Готово за {summary.elapsed:.1f} с: успешно {summary.ok} из {summary.total}; ошибок {summary.failed}; "
                f"пропущено {summary.skipped}; отменено {summary.cancelled}.")
        if problems:
            text += " ВНИМАНИЕ: контроль выявил расхождения."
        self.status.setText(text)
        warns = []
        for r in summary.results:
            for w in r.warnings:
                line = f"{r.src.name}: {w}"
                if line not in warns:
                    warns.append(line)
        self.notes.setPlainText("\n".join(problems + warns[:40]))
        self._update_buttons()
        self._refresh_history()
        self.ctx.message.emit(f"Конвертация завершена: {summary.ok} из {summary.total}")

    def _failed(self, msg: str) -> None:
        self.progress.hide()
        self._task = None
        self.status.setText("Ошибка пакетной обработки.")
        self.notes.setPlainText(msg)
        self._update_buttons()
        QMessageBox.critical(self, "Конвертер", msg)

    # ========================================================== результаты
    def _open_result(self, row: int) -> None:
        res = self.results.get(row)
        if res and res.outputs:
            open_path(str(res.outputs[0]))

    def _open_folder(self) -> None:
        if self._summary:
            for r in self._summary.results:
                if r.outputs:
                    open_path(str(r.outputs[0].parent))
                    return
        open_path(self.e_dir.text())

    # ============================================================== история
    def _refresh_history(self) -> None:
        self._history = self.cs.recent(40)
        self.hist.setRowCount(len(self._history))
        for r, rec in enumerate(self._history):
            vals = [rec.ts.replace("T", " "), rec.action, {"ok": "Готово", "error": "Ошибка", "warning": "Замечание"}.get(rec.status, rec.status),
                    ", ".join(Path(f).name for f in rec.files) or rec.message]
            for c, v in enumerate(vals):
                self.hist.setItem(r, c, QTableWidgetItem(v))
        self.hist.resizeColumnsToContents()
        self.hist.horizontalHeader().setStretchLastSection(True)

    def _open_history(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._history) and self._history[row].files:
            open_path(self._history[row].files[0])

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._refresh_history()
