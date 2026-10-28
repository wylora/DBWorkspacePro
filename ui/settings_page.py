"""Настройки (только реально применяемые на этом этапе) и диагностика окружения."""
from __future__ import annotations

import logging

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from utils.env_check import format_report, run_checks

from .context import AppContext
from .widgets import PageHeader


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        s = self.svc.settings
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.setSpacing(12)
        root.addWidget(PageHeader("Настройки", "Изменения применяются сразу и сохраняются между запусками."))

        card = QFrame()
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(20, 16, 20, 16)
        form.setVerticalSpacing(10)

        self.theme = QComboBox()
        self.theme.addItem("Светлая", "light")
        self.theme.addItem("Тёмная", "dark")
        self.theme.setCurrentIndex(self.theme.findData(s.get("theme")))
        self.theme.currentIndexChanged.connect(self._theme_changed)

        self.max_conn = self._spin("max_connections", 1, 20, " шт.")
        self.max_conn.setToolTip("Предельное число одновременно открытых соединений с серверами.")
        self.max_tasks = self._spin("max_parallel_tasks", 1, 16, " шт.")
        self.max_tasks.setToolTip("Сколько фоновых задач выполняется одновременно; остальные ждут в очереди.")
        self.batch = self._spin("fetch_batch_size", 10, 100_000, " строк")
        self.batch.setToolTip("Размер порции при чтении данных с сервера (fetchmany).")
        self.connect_to = self._spin("connect_timeout", 1, 300, " с")
        self.connect_to.setToolTip("Значение по умолчанию для новых профилей подключения.")
        self.query_to = self._spin("query_timeout", 0, 86400, " с")
        self.query_to.setSpecialValueText("без ограничения")
        self.query_to.setToolTip("Значение по умолчанию для новых профилей. 0 — без ограничения.")

        self.font_size = self._spin("editor_font_size", 8, 32, " пт")
        self.preview = self._spin("preview_row_limit", 1, 1_000_000, " строк")
        self.preview.setToolTip("Сколько строк результата хранится и показывается в таблице. Полный объём можно "
                                "выгрузить в файл потоком (без загрузки в память).")
        self.hist_on = self._check("query_history_enabled", "Сохранять историю запросов")
        self.hist_on.setToolTip("Текст запросов хранится локально; пароли и токены в нём маскируются. "
                                "Отключите, если в запросах бывают конфиденциальные данные.")
        self.hist_limit = self._spin("query_history_limit", 10, 100_000, " записей")
        self.guard = self._check("export_formula_guard", "Защищать экспорт от выполнения формул Excel")
        self.rep_dir = self._line("report_output_dir", "по умолчанию: Документы\\DB Workspace Pro\\Отчёты")
        b_rd = QPushButton("Обзор…")
        b_rd.clicked.connect(lambda: self._browse_dir(self.rep_dir, "report_output_dir"))
        self.rep_rows = self._spin("report_max_rows", 1, 100_000, " строк")
        self.rep_rows.setToolTip("Максимум строк на источник и таблицу в отчёте (значение по умолчанию).")
        self.rep_timeout = self._spin("report_pdf_timeout", 30, 1800, " с")
        self.rep_policy = QComboBox()
        for text, val in (("Добавлять номер к имени", "new_version"), ("Перезаписывать", "overwrite"), ("Пропускать", "skip")):
            self.rep_policy.addItem(text, val)
        self.rep_policy.setCurrentIndex(max(0, self.rep_policy.findData(s.get("report_overwrite"))))
        self.rep_policy.currentIndexChanged.connect(lambda: self._save_choice("report_overwrite", self.rep_policy.currentData()))
        self.lo_path = self._line("libreoffice_path", "пусто — искать автоматически; можно указать soffice.exe или папку")
        b_lo = QPushButton("Файл…")
        b_lo.clicked.connect(self._browse_lo)
        b_lo_dir = QPushButton("Папка…")
        b_lo_dir.clicked.connect(self._browse_lo_dir)
        b_lo_auto = QPushButton("Найти автоматически")
        b_lo_auto.clicked.connect(self._auto_lo)
        self.lo_status = QLabel("")
        self.lo_status.setWordWrap(True)
        self._update_lo_status()
        self.cv_dir = self._line("convert_output_dir", "по умолчанию: Документы\\DB Workspace Pro\\Конвертация")
        b_cv = QPushButton("Обзор…")
        b_cv.clicked.connect(lambda: self._browse_dir(self.cv_dir, "convert_output_dir"))
        self.cv_same = self._check("convert_same_dir", "Сохранять результат рядом с исходным файлом")
        self.cv_policy = QComboBox()
        for text, val in (("Добавлять номер к имени", "new_version"), ("Перезаписывать", "overwrite"), ("Пропускать", "skip")):
            self.cv_policy.addItem(text, val)
        self.cv_policy.setCurrentIndex(max(0, self.cv_policy.findData(s.get("convert_policy"))))
        self.cv_policy.currentIndexChanged.connect(lambda: self._save_choice("convert_policy", self.cv_policy.currentData()))
        self.cv_workers = self._spin("convert_workers", 1, 3, " потока")
        self.cv_workers.setToolTip("Сколько файлов преобразуется одновременно (каждый запуск LibreOffice занимает память).")
        self.cv_timeout = self._spin("convert_timeout", 30, 3600, " с")
        self.cv_timeout.setToolTip("Максимальное время одного преобразования через LibreOffice.")
        self.sched_on = self._check("scheduler_enabled", "Встроенный планировщик задач (работает, пока открыта программа)")
        self.sched_on.toggled.connect(self._on_scheduler)
        self.mon_int = self._spin("monitor_interval", 10, 3600, " с")
        self.mon_int.setToolTip("Как часто обновлять мониторинг при включённом автообновлении.")
        self.cmp_max = self._spin("compare_max_rows", 1_000, 2_000_000, " строк")
        self.cmp_max.setToolTip("Сравнение данных читает обе таблицы в память; больше — дольше и больше памяти.")
        self.doc_dir = self._line("doc_output_dir", "по умолчанию: Документы\\DB Workspace Pro\\Документация")
        b_dd = QPushButton("Обзор…")
        b_dd.clicked.connect(lambda: self._browse_dir(self.doc_dir, "doc_output_dir"))
        self.tray_on = self._check("tray_notifications", "Показывать ошибки и предупреждения задач в области уведомлений Windows")
        cfg = QWidget()
        cl = QHBoxLayout(cfg)
        cl.setContentsMargins(0, 0, 0, 0)
        for text, fn in (("Экспорт настроек…", self._export_cfg), ("Импорт настроек…", self._import_cfg),
                         ("Сбросить настройки…", self._reset_cfg)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            cl.addWidget(b)
        cl.addStretch(1)
        self.log_level = QComboBox()
        for lv in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.log_level.addItem(lv, lv)
        self.log_level.setCurrentIndex(self.log_level.findData(s.get("log_level")))
        self.log_level.currentIndexChanged.connect(self._log_level_changed)

        form.addRow("Тема оформления", self.theme)
        form.addRow("Макс. подключений", self.max_conn)
        form.addRow("Параллельных задач", self.max_tasks)
        form.addRow("Размер порции чтения", self.batch)
        form.addRow("Тайм-аут подключения (по умолчанию)", self.connect_to)
        form.addRow("Тайм-аут запроса (по умолчанию)", self.query_to)
        form.addRow("Размер шрифта редактора", self.font_size)
        form.addRow("Предпросмотр результата (макс.)", self.preview)
        form.addRow("История запросов", self.hist_on)
        form.addRow("Размер истории", self.hist_limit)
        form.addRow("Экспорт", self.guard)
        form.addRow("Папка отчётов", self._pair(self.rep_dir, b_rd))
        form.addRow("Если файл отчёта уже есть", self.rep_policy)
        form.addRow("Лимит строк в отчёте", self.rep_rows)
        form.addRow("Тайм-аут PDF (LibreOffice)", self.rep_timeout)
        lo_row = QWidget()
        lo_l = QHBoxLayout(lo_row)
        lo_l.setContentsMargins(0, 0, 0, 0)
        for w in (self.lo_path, b_lo, b_lo_dir, b_lo_auto):
            lo_l.addWidget(w, 1 if w is self.lo_path else 0)
        form.addRow("Путь к LibreOffice", lo_row)
        form.addRow("", self.lo_status)
        form.addRow("Конвертер: папка результата", self._pair(self.cv_dir, b_cv))
        form.addRow("", self.cv_same)
        form.addRow("Конвертер: если файл есть", self.cv_policy)
        form.addRow("Конвертер: параллельно", self.cv_workers)
        form.addRow("Конвертер: тайм-аут файла", self.cv_timeout)
        form.addRow("Автоматизация", self.sched_on)
        form.addRow("Интервал мониторинга", self.mon_int)
        form.addRow("Сравнение данных: не больше", self.cmp_max)
        form.addRow("Папка документации", self._pair(self.doc_dir, b_dd))
        form.addRow("Уведомления", self.tray_on)
        form.addRow("Резервная копия настроек", cfg)
        form.addRow("Уровень журнала", self.log_level)
        root.addWidget(card)

        info = QFrame()
        info.setObjectName("Card")
        il = QVBoxLayout(info)
        il.setContentsMargins(20, 14, 20, 14)
        il.addWidget(QLabel(f"Папка данных: {self.svc.paths.home}"))
        il.addWidget(QLabel(f"Хранилище паролей: {self.svc.credentials.backend_name()}"))
        row = QHBoxLayout()
        b_logs = QPushButton("Открыть папку журналов")
        b_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.svc.paths.logs))))
        self.b_diag = QPushButton("Проверить окружение")
        self.b_diag.clicked.connect(self._diagnose)
        row.addWidget(b_logs)
        row.addWidget(self.b_diag)
        row.addStretch(1)
        il.addLayout(row)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("Нажмите «Проверить окружение», чтобы увидеть состояние зависимостей.")
        self.report.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        il.addWidget(self.report)
        root.addWidget(info, 1)

    def _spin(self, key: str, lo: int, hi: int, suffix: str) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setSuffix(suffix)
        sp.setValue(self.svc.settings.get(key))
        sp.editingFinished.connect(lambda k=key, w=sp: self._save_int(k, w))
        return sp

    @staticmethod
    def _pair(a, b) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(a, 1)
        row.addWidget(b)
        return w

    def _line(self, key: str, placeholder: str = "") -> QLineEdit:
        e = QLineEdit(self.svc.settings.get(key))
        e.setPlaceholderText(placeholder)
        e.editingFinished.connect(lambda k=key, w=e: self._save_text(k, w.text().strip()))
        return e

    def _save_text(self, key: str, value: str) -> None:
        try:
            self.svc.settings.set(key, value)
        except ValueError as exc:
            self.ctx.message.emit(str(exc))
            return
        if key == "libreoffice_path":
            self._update_lo_status()
            self.ctx.settings_changed.emit()          # страницы отчётов и конвертера перепроверят LibreOffice
        self.ctx.message.emit("Настройка сохранена")

    def _save_choice(self, key: str, value: str) -> None:
        self.svc.settings.set(key, value)
        self.ctx.message.emit("Настройка сохранена")

    def _browse_dir(self, edit: QLineEdit, key: str) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка", edit.text())
        if d:
            edit.setText(d)
            self._save_text(key, d)

    def _browse_lo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Программа LibreOffice (soffice.exe в папке program)", "",
                                              "Программа (*.exe *.com soffice*);;Все файлы (*)")
        if path:
            self.lo_path.setText(path)
            self._save_text("libreoffice_path", path)

    def _browse_lo_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка LibreOffice (например, C:\\Program Files\\LibreOffice)")
        if d:
            self.lo_path.setText(d)
            self._save_text("libreoffice_path", d)

    def _auto_lo(self) -> None:
        self.lo_path.setText("")
        self._save_text("libreoffice_path", "")

    def _export_cfg(self) -> None:
        import datetime as _dt
        from pathlib import Path as _P
        from storage.config_backup import export_config
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт настроек",
                                              f"DBWorkspacePro_настройки_{_dt.datetime.now():%Y%m%d}.zip", "ZIP (*.zip)")
        if not path:
            return
        try:
            counts = export_config(self.svc, _P(path))
        except OSError as exc:
            QMessageBox.warning(self, "Экспорт настроек", str(exc))
            return
        QMessageBox.information(self, "Экспорт настроек", "Сохранено. Пароли в файл не входят.\n\n" +
                                "\n".join(f"{k}: {v}" for k, v in counts.items()))

    def _import_cfg(self) -> None:
        from pathlib import Path as _P
        from storage.config_backup import SECTIONS, ConfigBackupError, import_config, read_backup
        path, _ = QFileDialog.getOpenFileName(self, "Импорт настроек", "", "ZIP (*.zip)")
        if not path:
            return
        try:
            manifest, data = read_backup(_P(path))
        except ConfigBackupError as exc:
            QMessageBox.warning(self, "Импорт настроек", str(exc))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Импорт настроек")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"Файл создан {manifest.get('created', '?')} (версия {manifest.get('app_version', '?')}). "
                             "Выберите, что импортировать. Задачи по расписанию придут выключенными, подтверждения "
                             "SQL-сценариев будут сняты, пароли не переносятся."))
        boxes = {}
        for sec, title in SECTIONS.items():
            if sec in data:
                n = len(data[sec]) if isinstance(data[sec], (list, dict)) else 0
                cb = QCheckBox(f"{title}: {n}")
                cb.setChecked(sec != "settings")
                boxes[sec] = cb
                lay.addWidget(cb)
        over = QCheckBox("Заменять существующие объекты с тем же идентификатором")
        over.setChecked(True)
        lay.addWidget(over)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if not dlg.exec():
            return
        chosen = {k for k, cb in boxes.items() if cb.isChecked()}
        try:
            rep = import_config(self.svc, _P(path), chosen, overwrite=over.isChecked())
        except ConfigBackupError as exc:
            QMessageBox.warning(self, "Импорт настроек", str(exc))
            return
        self.ctx.connections_changed.emit()
        self.ctx.reports_changed.emit()
        self.ctx.settings_changed.emit()
        QMessageBox.information(self, "Импорт настроек", rep.summary() + ("\n\n" + "\n".join(rep.warnings[:15]) if rep.warnings else "")
                                + "\n\nНекоторые настройки применятся после перезапуска программы.")

    def _reset_cfg(self) -> None:
        from storage.config_backup import reset_settings
        if QMessageBox.question(self, "Сброс настроек", "Вернуть все настройки к значениям по умолчанию? Подключения, "
                                "шаблоны, сценарии и задачи не затрагиваются.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        n = reset_settings(self.svc)
        self.ctx.settings_changed.emit()
        QMessageBox.information(self, "Сброс настроек", f"Изменено значений: {n}. Часть настроек применится после перезапуска.")

    def _on_scheduler(self, on: bool) -> None:
        sch = self.ctx.automation.scheduler
        if on:
            sch.start()
        else:
            sch.stop()

    def _update_lo_status(self) -> None:
        from converter.libreoffice import locate_soffice
        lo = locate_soffice(self.svc.settings.get("libreoffice_path"))
        if lo.path:
            text = f"LibreOffice найден: {lo.path} ({lo.source})." + (f" {lo.note}" if lo.note else "")
        else:
            text = ("LibreOffice не найден — PDF и форматы DOC/ODT/RTF/XLS недоступны. Установите его (libreoffice.org) "
                    "или укажите файл soffice.exe либо папку LibreOffice. " + (lo.note + " " if lo.note else "")
                    + "Где искали: " + "; ".join(lo.checked[:8]) + ("…" if len(lo.checked) > 8 else ""))
        self.lo_status.setText(text)
        self.lo_status.setToolTip("Проверено:\n" + "\n".join(lo.checked))

    def _check(self, key: str, text: str) -> QCheckBox:
        cb = QCheckBox(text)
        cb.setChecked(self.svc.settings.get(key))
        cb.toggled.connect(lambda on, k=key: self._save_bool(k, on))
        return cb

    def _save_bool(self, key: str, value: bool) -> None:
        self.svc.settings.set(key, value)
        self.ctx.message.emit("Настройка сохранена")

    def _save_int(self, key: str, widget: QSpinBox) -> None:
        value = widget.value()
        try:
            self.svc.settings.set(key, value)
        except ValueError as exc:
            self.ctx.message.emit(str(exc))
            return
        if key == "max_connections":
            self.svc.manager.set_max_connections(value)
        elif key == "max_parallel_tasks":
            self.svc.tasks.set_max_parallel(value)
        elif key == "query_history_limit":
            self.svc.history.limit = value
        elif key == "editor_font_size":
            self.ctx.settings_changed.emit()
        self.ctx.message.emit("Настройка сохранена")

    def _theme_changed(self) -> None:
        name = self.theme.currentData()
        self.svc.settings.set("theme", name)
        self.ctx.theme_changed.emit(name)

    def _log_level_changed(self) -> None:
        level = self.log_level.currentData()
        self.svc.settings.set("log_level", level)
        logging.getLogger().setLevel(getattr(logging, level))
        self.ctx.message.emit(f"Уровень журнала: {level}")

    def _diagnose(self) -> None:
        self.b_diag.setEnabled(False)
        self.report.setPlainText("Проверка…")

        def done(text):
            self.report.setPlainText(text)
            self.b_diag.setEnabled(True)

        def failed(msg):
            self.report.setPlainText(f"Не удалось выполнить проверку: {msg}")
            self.b_diag.setEnabled(True)

        try:
            self.ctx.runner.submit("Проверка окружения",
                                   lambda c: format_report(run_checks(self.svc.paths.home)),
                                   on_success=done, on_error=failed, key="env-check")
        except Exception as exc:  # noqa: BLE001
            failed(str(exc))
