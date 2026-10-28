"""Автоматизация: задачи по расписанию, SQL-сценарии, резервные копии, уведомления."""
from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from automation import backup as bk
from automation.jobs import ON_CONNECT_KINDS
from automation.schedule import KINDS as SCHEDULE_KINDS, WEEKDAYS_RU, Schedule, describe
from automation.scripts import Scenario, approve, check_permission, run_scenario
from automation.service import STATUS_RU
from automation.store import TASK_KINDS, ScheduledTask
from automation.winsched import schtasks_create, supported, task_xml, launch_command, write_xml
from database.errors import DbWorkspaceError
from database.sql_guard import RiskLevel, describe as describe_risk
from reports.model import FORMAT_TITLES, FORMATS
from utils.tasks import DuplicateTaskError

from .code_editor import CodeEditor
from .context import AppContext
from .table_editor import Col, TableEditor
from .widgets import PageHeader

log = logging.getLogger("dbworkspace.automation")
SCHEDULE_TITLES = {"once": "Однократно", "interval": "Через интервал", "daily": "Ежедневно", "weekly": "Еженедельно",
                   "monthly": "Ежемесячно", "on_connect": "После подключения"}
NOTIFY_TITLES = [("Только об ошибках и замечаниях", "errors"), ("Всегда", "always"), ("Никогда", "never")]
LEVEL_RU = {"info": "Информация", "success": "Успех", "warning": "Внимание", "error": "Ошибка"}
CONFIRM_WORD = "ВЫПОЛНИТЬ"


def lines(text: str) -> list[str]:
    return [x.strip() for x in text.replace(",", "\n").splitlines() if x.strip()]


def parse_pairs(text: str, sep: str = "=") -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    return t


def fill(t: QTableWidget, rows: list[list]) -> None:
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
    t.resizeColumnsToContents()
    t.horizontalHeader().setStretchLastSection(True)


def profile_combo(ctx: AppContext) -> QComboBox:
    c = QComboBox()
    for p in ctx.services.profiles.repo.list():
        c.addItem(p.name, p.id)
    return c


def dir_row(edit: QLineEdit, parent: QWidget) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)
    b = QPushButton("Обзор…")
    b.clicked.connect(lambda: (lambda d: edit.setText(d) if d else None)(
        QFileDialog.getExistingDirectory(parent, "Папка", edit.text())))
    lay.addWidget(b)
    return w


# ================================================================ редактор задачи
class TaskDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None, task: ScheduledTask | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.auto = ctx.automation
        self.task = task or ScheduledTask.new("Новая задача", "health", Schedule("daily", time="08:00"))
        self.setWindowTitle("Задача автоматизации")
        self.resize(720, 640)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.e_name = QLineEdit()
        self.c_kind = QComboBox()
        for k, title in TASK_KINDS.items():
            self.c_kind.addItem(title, k)
        self.c_kind.currentIndexChanged.connect(self._on_kind)
        self.k_enabled = QCheckBox("Задача включена")
        f.addRow("Название", self.e_name)
        f.addRow("Что делать", self.c_kind)
        f.addRow("", self.k_enabled)
        lay.addLayout(f)

        sg = QGroupBox("Расписание")
        sf = QFormLayout(sg)
        self.c_sched = QComboBox()
        for k in SCHEDULE_KINDS:
            self.c_sched.addItem(SCHEDULE_TITLES[k], k)
        self.c_sched.currentIndexChanged.connect(self._on_sched)
        self.e_time = QLineEdit("08:00")
        self.e_time.setPlaceholderText("ЧЧ:ММ")
        self.days = []
        drow = QHBoxLayout()
        for i, d in enumerate(WEEKDAYS_RU):
            cb = QCheckBox(d)
            self.days.append(cb)
            drow.addWidget(cb)
        dwrap = QWidget()
        dwrap.setLayout(drow)
        self.s_day = QSpinBox()
        self.s_day.setRange(1, 31)
        self.s_interval = QSpinBox()
        self.s_interval.setRange(1, 7 * 24 * 60)
        self.s_interval.setSuffix(" мин")
        self.e_at = QLineEdit()
        self.e_at.setPlaceholderText("ГГГГ-ММ-ДД ЧЧ:ММ")
        self.k_catch = QCheckBox("Если запуск пропущен (программа была закрыта) — выполнить при следующем запуске программы")
        self.c_notify = QComboBox()
        for text, val in NOTIFY_TITLES:
            self.c_notify.addItem(text, val)
        for label, w in (("Когда", self.c_sched), ("Время", self.e_time), ("Дни недели", dwrap), ("День месяца", self.s_day),
                         ("Интервал", self.s_interval), ("Дата и время", self.e_at), ("", self.k_catch),
                         ("Уведомлять", self.c_notify)):
            sf.addRow(label, w)
        self._sched_rows = {"time": self.e_time, "days": dwrap, "day": self.s_day, "interval": self.s_interval, "at": self.e_at}
        lay.addWidget(sg)

        self.stack = QStackedWidget()
        self.pages: dict[str, dict] = {}
        for kind in TASK_KINDS:
            w, fields = self._kind_page(kind)
            self.pages[kind] = fields
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        self.lbl_errors = QLabel("")
        self.lbl_errors.setWordWrap(True)
        self.lbl_errors.setStyleSheet("color: #D32F2F;")
        lay.addWidget(self.lbl_errors)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.load(self.task)

    def _kind_page(self, kind: str):
        w = QGroupBox("Параметры: " + TASK_KINDS[kind])
        f = QFormLayout(w)
        x: dict = {}
        if kind != "script":
            x["profile"] = profile_combo(self.ctx)
            f.addRow("Подключение", x["profile"])
        if kind == "report":
            x["template"] = QComboBox()
            for i in self.ctx.reports.templates.list():
                x["template"].addItem(i.name, i.id)
            x["database"] = QLineEdit()
            fmts = QHBoxLayout()
            x["formats"] = {}
            for fm in FORMATS:
                cb = QCheckBox(FORMAT_TITLES[fm])
                x["formats"][fm] = cb
                fmts.addWidget(cb)
            fw = QWidget()
            fw.setLayout(fmts)
            x["output"] = QLineEdit()
            x["policy"] = QComboBox()
            for t_, v in (("Новая версия имени", "new_version"), ("Перезаписывать", "overwrite"), ("Пропускать", "skip")):
                x["policy"].addItem(t_, v)
            x["params"] = QPlainTextEdit()
            x["params"].setPlaceholderText("имя=значение — по одному на строку")
            x["params"].setMaximumHeight(70)
            x["history"] = QCheckBox("Хранить снимок данных и сравнивать с прошлым запуском")
            x["keys"] = QPlainTextEdit()
            x["keys"].setPlaceholderText("источник: ключевой_столбец[, ещё] — для построчного сравнения")
            x["keys"].setMaximumHeight(60)
            for label, wd in (("Шаблон", x["template"]), ("База данных", x["database"]), ("Форматы", fw),
                              ("Папка", dir_row(x["output"], self)), ("Если файл есть", x["policy"]),
                              ("Параметры", x["params"]), ("", x["history"]), ("Ключи сравнения", x["keys"])):
                f.addRow(label, wd)
        elif kind == "export":
            x["database"] = QLineEdit()
            x["tables"] = QPlainTextEdit()
            x["tables"].setPlaceholderText("схема.таблица — по одной на строку")
            x["tables"].setMaximumHeight(90)
            x["format"] = QComboBox()
            for v in ("csv", "xlsx", "json"):
                x["format"].addItem(v.upper(), v)
            x["output"] = QLineEdit()
            x["max_rows"] = QSpinBox()
            x["max_rows"].setRange(0, 10_000_000)
            x["max_rows"].setSpecialValueText("все строки")
            for label, wd in (("База данных", x["database"]), ("Таблицы", x["tables"]), ("Формат", x["format"]),
                              ("Папка", dir_row(x["output"], self)), ("Строк не больше", x["max_rows"])):
                f.addRow(label, wd)
        elif kind == "backup":
            x["databases"] = QPlainTextEdit()
            x["databases"].setPlaceholderText("имя базы — по одному на строку")
            x["databases"].setMaximumHeight(70)
            x["folder"] = QLineEdit()
            x["folder"].setPlaceholderText("папка НА СЕРВЕРЕ, например D:\\Backup")
            x["copy_only"] = QCheckBox("COPY_ONLY (не нарушать цепочку копий администратора)")
            x["checksum"] = QCheckBox("Контрольные суммы (CHECKSUM)")
            x["verify"] = QCheckBox("Проверять копию (RESTORE VERIFYONLY)")
            x["compression"] = QCheckBox("Сжатие (недоступно в Express)")
            x["cleanup"] = QCheckBox("Удалять старые копии этой программы")
            x["local"] = QLineEdit()
            x["local"].setPlaceholderText("как эта папка видна с компьютера (общая папка), если отличается")
            x["keep"] = QSpinBox()
            x["keep"].setRange(1, 1000)
            x["older"] = QSpinBox()
            x["older"].setRange(0, 3650)
            x["older"].setSpecialValueText("без ограничения по возрасту")
            x["older"].setSuffix(" дн.")
            x["ack"] = QCheckBox("Понимаю: файлы копий старше указанных будут удаляться безвозвратно")
            for label, wd in (("Базы данных", x["databases"]), ("Папка на сервере", x["folder"]), ("", x["copy_only"]),
                              ("", x["checksum"]), ("", x["verify"]), ("", x["compression"]), ("", x["cleanup"]),
                              ("Папка с компьютера", x["local"]), ("Хранить последних", x["keep"]),
                              ("И удалять старше", x["older"]), ("", x["ack"])):
                f.addRow(label, wd)
        elif kind == "health":
            x["latency"] = QSpinBox()
            x["latency"].setRange(1, 600_000)
            x["latency"].setSuffix(" мс")
            f.addRow("Предупреждать, если отклик больше", x["latency"])
        elif kind == "table_stats":
            x["database"] = QLineEdit()
            x["top"] = QSpinBox()
            x["top"].setRange(1, 500)
            f.addRow("База данных", x["database"])
            f.addRow("Сколько крупнейших таблиц", x["top"])
        elif kind == "script":
            x["scenario"] = QComboBox()
            for s in self.auto.scenarios.list():
                x["scenario"].addItem(s.name, s.id)
            x["params"] = QPlainTextEdit()
            x["params"].setPlaceholderText("имя=значение — по одному на строку")
            x["params"].setMaximumHeight(70)
            f.addRow("SQL-сценарий", x["scenario"])
            f.addRow("Параметры", x["params"])
        return w, x

    def _on_kind(self, _i=None) -> None:
        self.stack.setCurrentIndex(list(TASK_KINDS).index(self.c_kind.currentData()))

    def _on_sched(self, _i=None) -> None:
        k = self.c_sched.currentData()
        vis = {"time": k in ("daily", "weekly", "monthly"), "days": k == "weekly", "day": k == "monthly",
               "interval": k == "interval", "at": k == "once"}
        for key, w in self._sched_rows.items():
            w.setEnabled(vis[key])

    def load(self, t: ScheduledTask) -> None:
        self.e_name.setText(t.name)
        self.c_kind.setCurrentIndex(max(0, self.c_kind.findData(t.kind)))
        self.k_enabled.setChecked(t.enabled)
        s = t.schedule
        self.c_sched.setCurrentIndex(max(0, self.c_sched.findData(s.kind)))
        self.e_time.setText(s.time)
        for i, cb in enumerate(self.days):
            cb.setChecked(i in s.weekdays)
        self.s_day.setValue(int(s.day))
        self.s_interval.setValue(int(s.interval_minutes))
        self.e_at.setText(s.at.replace("T", " ")[:16])
        self.k_catch.setChecked(t.catch_up)
        c = t.config
        self.c_notify.setCurrentIndex(max(0, self.c_notify.findData(c.get("notify", "errors"))))
        x = self.pages[t.kind]
        if "profile" in x:
            x["profile"].setCurrentIndex(max(0, x["profile"].findData(c.get("profile_id"))))
        if t.kind == "report":
            x["template"].setCurrentIndex(max(0, x["template"].findData(c.get("template_id"))))
            x["database"].setText(c.get("database", ""))
            for fm, cb in x["formats"].items():
                cb.setChecked(fm in (c.get("formats") or ["docx"]))
            x["output"].setText(c.get("output_dir", str(self.ctx.reports.output_dir())))
            x["policy"].setCurrentIndex(max(0, x["policy"].findData(c.get("policy", "new_version"))))
            x["params"].setPlainText("\n".join(f"{k}={v}" for k, v in (c.get("params") or {}).items()))
            x["history"].setChecked(bool(c.get("keep_history")))
            x["keys"].setPlainText("\n".join(f"{k}: {', '.join(v)}" for k, v in (c.get("compare_keys") or {}).items()))
        elif t.kind == "export":
            x["database"].setText(c.get("database", ""))
            x["tables"].setPlainText("\n".join(c.get("tables") or []))
            x["format"].setCurrentIndex(max(0, x["format"].findData(c.get("format", "csv"))))
            x["output"].setText(c.get("output_dir", ""))
            x["max_rows"].setValue(int(c.get("max_rows") or 0))
        elif t.kind == "backup":
            x["databases"].setPlainText("\n".join(c.get("databases") or []))
            x["folder"].setText(c.get("folder", ""))
            for k_ in ("copy_only", "checksum", "verify"):
                x[k_].setChecked(bool(c.get(k_, True)))
            x["compression"].setChecked(bool(c.get("compression", False)))
            cl = c.get("cleanup") or {}
            x["cleanup"].setChecked(bool(cl.get("enabled")))
            x["local"].setText(cl.get("local_folder", ""))
            x["keep"].setValue(int(cl.get("keep_last", 7)))
            x["older"].setValue(int(cl.get("older_than_days", 0)))
            x["ack"].setChecked(bool(cl.get("acknowledged")))
        elif t.kind == "health":
            x["latency"].setValue(int(c.get("latency_warn_ms", 1000)))
        elif t.kind == "table_stats":
            x["database"].setText(c.get("database", ""))
            x["top"].setValue(int(c.get("top", 20)))
        elif t.kind == "script":
            x["scenario"].setCurrentIndex(max(0, x["scenario"].findData(c.get("scenario_id"))))
            x["params"].setPlainText("\n".join(f"{k}={v}" for k, v in (c.get("params") or {}).items()))
        self._on_kind()
        self._on_sched()

    def collect(self) -> ScheduledTask:
        kind = self.c_kind.currentData()
        sched = Schedule(self.c_sched.currentData(), self.e_time.text().strip(),
                         [i for i, cb in enumerate(self.days) if cb.isChecked()], self.s_day.value(),
                         self.s_interval.value(), self.e_at.text().strip())
        x = self.pages[kind]
        c: dict = {"notify": self.c_notify.currentData()}
        if "profile" in x:
            c["profile_id"] = x["profile"].currentData() or ""
        if kind == "report":
            c.update(template_id=x["template"].currentData() or "", database=x["database"].text().strip(),
                     formats=[fm for fm, cb in x["formats"].items() if cb.isChecked()], output_dir=x["output"].text().strip(),
                     policy=x["policy"].currentData(), params=parse_pairs(x["params"].toPlainText()),
                     keep_history=x["history"].isChecked(),
                     compare_keys={k: lines(v) for k, v in parse_pairs(x["keys"].toPlainText(), ":").items()})
        elif kind == "export":
            c.update(database=x["database"].text().strip(), tables=[l for l in x["tables"].toPlainText().splitlines() if l.strip()],
                     format=x["format"].currentData(), output_dir=x["output"].text().strip(), max_rows=x["max_rows"].value())
        elif kind == "backup":
            c.update(databases=[l.strip() for l in x["databases"].toPlainText().splitlines() if l.strip()],
                     folder=x["folder"].text().strip(), copy_only=x["copy_only"].isChecked(),
                     checksum=x["checksum"].isChecked(), verify=x["verify"].isChecked(),
                     compression=x["compression"].isChecked(),
                     cleanup={"enabled": x["cleanup"].isChecked(), "local_folder": x["local"].text().strip(),
                              "keep_last": x["keep"].value(), "older_than_days": x["older"].value(),
                              "acknowledged": x["ack"].isChecked()})
        elif kind == "health":
            c["latency_warn_ms"] = x["latency"].value()
        elif kind == "table_stats":
            c.update(database=x["database"].text().strip(), top=x["top"].value())
        elif kind == "script":
            c.update(scenario_id=x["scenario"].currentData() or "", params=parse_pairs(x["params"].toPlainText()))
        t = ScheduledTask(self.task.id, self.e_name.text().strip(), kind, sched, c, self.k_enabled.isChecked(),
                          self.k_catch.isChecked(), self.task.next_run, self.task.last_run, self.task.last_status)
        return t

    def _save(self) -> None:
        t = self.collect()
        errs = self.auto.save_task(t)
        if errs:
            self.lbl_errors.setText("• " + "\n• ".join(errs))
            return
        self.task = t
        self.accept()


class WindowsTaskDialog(QDialog):
    def __init__(self, parent, task: ScheduledTask):
        super().__init__(parent)
        self.task = task
        self.setWindowTitle("Задание Планировщика Windows")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        ok, why = supported(task.schedule)
        info = QLabel(
            "Встроенный планировщик работает, только пока программа открыта. Задание Планировщика Windows запустит "
            "эту задачу по расписанию, даже когда программа закрыта (выполняется от вашего имени, когда вы вошли в "
            "систему; для подключений с паролем SQL Server пароль должен быть сохранён).")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.xml = QPlainTextEdit()
        self.xml.setReadOnly(True)
        self.cmd = QLineEdit()
        self.cmd.setReadOnly(True)
        if ok:
            command, args, wd = launch_command(task.id)
            self.xml.setPlainText(task_xml(task, command, args, wd))
            self.cmd.setText(" ".join(f'"{a}"' if " " in a else a for a in schtasks_create(task, Path("<файл.xml>"))))
        else:
            self.xml.setPlainText(why)
        lay.addWidget(QLabel("Описание задания (XML):"))
        lay.addWidget(self.xml, 1)
        lay.addWidget(QLabel("Команда для создания задания:"))
        lay.addWidget(self.cmd)
        row = QHBoxLayout()
        self.b_save = QPushButton("Сохранить XML…")
        self.b_save.clicked.connect(self._save_xml)
        self.b_create = QPushButton("Создать задание в Windows…")
        self.b_create.clicked.connect(self._create)
        self.b_create.setEnabled(ok and sys.platform == "win32")
        if sys.platform != "win32":
            self.b_create.setToolTip("Доступно только в Windows")
        self.b_save.setEnabled(ok)
        row.addWidget(self.b_save)
        row.addWidget(self.b_create)
        row.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)

    def _save_xml(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить XML", f"{self.task.name}.xml", "XML (*.xml)")
        if path:
            write_xml(self.task, Path(path))

    def _create(self) -> None:
        a = QMessageBox.question(self, "Создать задание Windows",
                                 "Будет создано задание в Планировщике Windows от вашего имени. Продолжить?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if a != QMessageBox.StandardButton.Yes:
            return
        with tempfile.TemporaryDirectory() as d:
            xml = write_xml(self.task, Path(d) / "task.xml")
            res = subprocess.run(schtasks_create(self.task, xml), capture_output=True, text=True,
                                 creationflags=0x08000000 if sys.platform == "win32" else 0)
        if res.returncode == 0:
            QMessageBox.information(self, "Готово", "Задание создано в Планировщике Windows (папка DBWorkspacePro).")
        else:
            QMessageBox.warning(self, "Не удалось", (res.stderr or res.stdout or "Ошибка schtasks").strip())


class RestoreDialog(QDialog):
    """Восстановление копии в НОВУЮ базу. Существующие базы не затрагиваются."""

    def __init__(self, parent, path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Восстановить копию как новую базу")
        self.resize(560, 300)
        lay = QVBoxLayout(self)
        warn = QLabel("Копия будет восстановлена в НОВУЮ базу с указанным именем (существующие базы не изменяются). "
                      "Для замены существующей базы используйте инструкцию по восстановлению — это делается вручную.")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        f = QFormLayout()
        self.e_path = QLineEdit(path)
        self.e_path.setPlaceholderText("путь к .bak на сервере")
        self.e_name = QLineEdit()
        self.e_confirm = QLineEdit()
        self.e_confirm.setPlaceholderText("введите имя новой базы ещё раз")
        f.addRow("Файл копии", self.e_path)
        f.addRow("Имя новой базы", self.e_name)
        f.addRow("Подтверждение", self.e_confirm)
        lay.addLayout(f)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _ok(self) -> None:
        if not self.e_path.text().strip() or not self.e_name.text().strip():
            QMessageBox.information(self, "Восстановление", "Укажите файл копии и имя новой базы.")
            return
        if self.e_confirm.text().strip() != self.e_name.text().strip():
            QMessageBox.warning(self, "Восстановление", "Подтверждение не совпадает с именем новой базы.")
            return
        self.accept()


# ======================================================================= страница
class AutomationPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self.auto = ctx.automation
        self._tasks: list[ScheduledTask] = []
        self._runs = []
        self._scenarios: list[Scenario] = []
        self._scn: Scenario | None = None
        self._notes = []
        self._build()
        ctx.automation_changed.connect(lambda _tid: self.refresh_tasks())
        ctx.notifications_changed.connect(self.refresh_notes)
        ctx.connections_changed.connect(self._reload_profiles)
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.refresh_tasks)
        self.timer.start()
        self.refresh_all()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Автоматизация",
            "Задачи по расписанию, SQL-сценарии, резервное копирование и уведомления. Встроенный планировщик работает, "
            "пока открыта программа; для запуска при закрытой программе создайте задание Планировщика Windows."))
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._tab_tasks(), "Задачи")
        self.tabs.addTab(self._tab_scenarios(), "SQL-сценарии")
        self.tabs.addTab(self._tab_backup(), "Резервные копии")
        self.tabs.addTab(self._tab_notes(), "Уведомления")

    # ---------------------------------------------------------------- задачи
    def _tab_tasks(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_sched = QLabel("")
        self.lbl_sched.setObjectName("Muted")
        self.lbl_sched.setWordWrap(True)
        lay.addWidget(self.lbl_sched)
        row = QHBoxLayout()
        for text, fn in (("＋ Создать…", self.create_task), ("Изменить…", self.edit_task), ("Удалить", self.delete_task),
                         ("Вкл./выкл.", self.toggle_task), ("Запустить сейчас", self.run_task),
                         ("Планировщик Windows…", self.windows_task)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.t_tasks = table(["Название", "Что делает", "Расписание", "Следующий запуск", "Последний", "Результат", "Вкл."])
        self.t_tasks.itemSelectionChanged.connect(self._on_task_selected)
        self.t_tasks.cellDoubleClicked.connect(lambda *_: self.edit_task())
        split.addWidget(self.t_tasks)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(QLabel("История запусков выбранной задачи:"))
        self.t_runs = table(["Начало", "Окончание", "Как запущена", "Результат", "Сообщение"])
        self.t_runs.itemSelectionChanged.connect(self._on_run_selected)
        bl.addWidget(self.t_runs, 1)
        self.run_details = QPlainTextEdit()
        self.run_details.setReadOnly(True)
        self.run_details.setMaximumHeight(120)
        bl.addWidget(self.run_details)
        split.addWidget(bottom)
        split.setSizes([260, 300])
        lay.addWidget(split, 1)
        return w

    def refresh_tasks(self) -> None:
        sel = self._selected_task()
        self._tasks = self.auto.tasks.list()
        rows = []
        for t in self._tasks:
            state = "выполняется" if self.auto.is_running(t.id) else STATUS_RU.get(t.last_status or "", "—")
            rows.append([t.name, TASK_KINDS.get(t.kind, t.kind), describe(t.schedule),
                         (t.next_run or "—").replace("T", " ") if t.enabled else "выключена",
                         (t.last_run or "—").replace("T", " "), state, "да" if t.enabled else "нет"])
        fill(self.t_tasks, rows)
        if sel is not None:
            for i, t in enumerate(self._tasks):
                if t.id == sel.id:
                    self.t_tasks.selectRow(i)
        on = self.auto.scheduler.running
        self.lbl_sched.setText(("Встроенный планировщик работает (пока открыта программа)." if on else
                                "Встроенный планировщик выключен в настройках — задачи запускаются только вручную.") +
                               " Пропущенные из-за закрытой программы запуски отмечаются в истории.")
        self._on_task_selected()

    def _selected_task(self) -> ScheduledTask | None:
        r = self.t_tasks.currentRow()
        return self._tasks[r] if 0 <= r < len(self._tasks) else None

    def _on_task_selected(self) -> None:
        t = self._selected_task()
        self._runs = self.auto.runs.recent(t.id, 100) if t else []
        fill(self.t_runs, [[r.started.replace("T", " "), (r.finished or "").replace("T", " "),
                            {"schedule": "по расписанию", "manual": "вручную", "missed": "пропуск", "catch_up": "догоняющий",
                             "on_connect": "после подключения", "windows": "Планировщик Windows", "cli": "командная строка"}
                            .get(r.trigger, r.trigger), STATUS_RU.get(r.status, r.status), r.message] for r in self._runs])
        self.run_details.clear()

    def _on_run_selected(self) -> None:
        r = self.t_runs.currentRow()
        if 0 <= r < len(self._runs):
            rec = self._runs[r]
            self.run_details.setPlainText(rec.message + ("\n\n" + rec.details if rec.details else ""))

    def create_task(self) -> None:
        if not self.svc.profiles.repo.list():
            QMessageBox.information(self, "Автоматизация", "Сначала создайте подключение на странице «Подключения».")
            return
        if TaskDialog(self.ctx, self).exec():
            self.refresh_tasks()

    def edit_task(self) -> None:
        t = self._selected_task()
        if t and TaskDialog(self.ctx, self, t).exec():
            self.refresh_tasks()

    def delete_task(self) -> None:
        t = self._selected_task()
        if not t:
            return
        if QMessageBox.question(self, "Удаление задачи", f"Удалить задачу «{t.name}» и её историю?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.auto.delete_task(t.id)
            self.refresh_tasks()

    def toggle_task(self) -> None:
        t = self._selected_task()
        if t:
            self.auto.set_enabled(t.id, not t.enabled)
            self.refresh_tasks()

    def run_task(self) -> None:
        t = self._selected_task()
        if not t:
            return
        if t.kind == "backup" and QMessageBox.question(
                self, "Резервное копирование", "Запустить резервное копирование сейчас?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        if not self.auto.run_now(t.id):
            QMessageBox.information(self, "Автоматизация", "Задача уже выполняется.")
        self.refresh_tasks()

    def windows_task(self) -> None:
        t = self._selected_task()
        if t:
            WindowsTaskDialog(self, t).exec()

    # --------------------------------------------------------------- сценарии
    def _tab_scenarios(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.l_scn = QListWidget()
        self.l_scn.setMaximumWidth(230)
        self.l_scn.currentRowChanged.connect(self._on_scn_row)
        left.addWidget(self.l_scn, 1)
        for text, fn in (("＋ Новый", self.new_scenario), ("Импорт .sql…", self.import_scenario), ("Удалить", self.delete_scenario)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            left.addWidget(b)
        lay.addLayout(left)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        f = QFormLayout()
        self.s_name = QLineEdit()
        self.s_profile = profile_combo(self.ctx)
        self.s_db = QLineEdit()
        self.s_db.setPlaceholderText("необязательно: база данных (USE)")
        f.addRow("Название", self.s_name)
        f.addRow("Подключение", self.s_profile)
        f.addRow("База данных", self.s_db)
        cl.addLayout(f)
        self.s_sql = CodeEditor(self.ctx.theme, self.svc.settings.get("editor_font_size"))
        self.s_sql.setMinimumHeight(160)
        self.s_sql.textChanged.connect(self._update_scn_status)
        cl.addWidget(self.s_sql, 1)
        self.s_params = TableEditor([Col("name", "Параметр :имя", width=130), Col("label", "Подпись", width=140),
                                     Col("type", "Тип", "combo", [("Текст", "text"), ("Целое", "int"), ("Число", "decimal"),
                                                                  ("Дата", "date"), ("Да/нет", "bool")], 110, "text"),
                                     Col("default", "Значение", width=120), Col("required", "Обяз.", "check", default=False)],
                                    min_height=90)
        cl.addWidget(self.s_params)
        opts = QHBoxLayout()
        self.k_mod = QCheckBox("Разрешить изменения")
        self.k_mod.toggled.connect(self._on_allow_mod)
        self.k_tx = QCheckBox("В транзакции (откат при ошибке)")
        self.s_timeout = QSpinBox()
        self.s_timeout.setRange(0, 86400)
        self.s_timeout.setSpecialValueText("тайм-аут профиля")
        self.s_timeout.setSuffix(" с")
        self.k_save = QCheckBox("Сохранять результат в CSV:")
        self.s_dir = QLineEdit()
        for wd in (self.k_mod, self.k_tx, self.s_timeout, self.k_save):
            opts.addWidget(wd)
        opts.addWidget(dir_row(self.s_dir, self), 1)
        cl.addLayout(opts)
        self.lbl_scn = QLabel("")
        self.lbl_scn.setWordWrap(True)
        cl.addWidget(self.lbl_scn)
        row = QHBoxLayout()
        for text, fn in (("Сохранить", self.save_scenario), ("Подтвердить текст SQL…", self.approve_scenario),
                         ("Выполнить сейчас", self.run_scenario_now)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        cl.addLayout(row)
        self.t_scn_runs = table(["Время", "Результат", "Длит., мс", "Строк", "Сообщение"])
        self.t_scn_runs.setMaximumHeight(150)
        cl.addWidget(self.t_scn_runs)
        lay.addWidget(card, 1)
        return w

    def refresh_scenarios(self, select: str | None = None) -> None:
        self._scenarios = self.auto.scenarios.list()
        self.l_scn.blockSignals(True)
        self.l_scn.clear()
        for s in self._scenarios:
            self.l_scn.addItem(s.name)
        self.l_scn.blockSignals(False)
        idx = next((i for i, s in enumerate(self._scenarios) if s.id == select), 0 if self._scenarios else -1)
        if idx >= 0:
            self.l_scn.setCurrentRow(idx)
            self._load_scn(self._scenarios[idx])
        else:
            self._load_scn(None)

    def _on_scn_row(self, row: int) -> None:
        if 0 <= row < len(self._scenarios):
            self._load_scn(self._scenarios[row])

    def _load_scn(self, s: Scenario | None) -> None:
        self._scn = s
        s = s or Scenario.new("", "")
        self.s_name.setText(s.name)
        self.s_profile.setCurrentIndex(max(0, self.s_profile.findData(s.profile_id)))
        self.s_db.setText(s.database)
        self.s_sql.setPlainText(s.sql)
        self.s_params.set_rows(s.params)
        self.k_mod.blockSignals(True)
        self.k_mod.setChecked(s.allow_modifications)
        self.k_mod.blockSignals(False)
        self.k_tx.setChecked(s.use_transaction)
        self.s_timeout.setValue(s.timeout)
        self.k_save.setChecked(s.save_results)
        self.s_dir.setText(s.results_dir)
        self._update_scn_status()
        runs = self.auto.scenarios.runs(self._scn.id) if self._scn else []
        fill(self.t_scn_runs, [[r["ts"].replace("T", " "), STATUS_RU.get(r["status"], r["status"]), r["duration_ms"],
                                r["rows"], r["message"]] for r in runs])

    def _collect_scn(self) -> Scenario:
        base = self._scn or Scenario.new()
        s = Scenario(base.id, self.s_name.text().strip() or "Сценарий", self.s_sql.toPlainText(),
                     self.s_profile.currentData() or "", self.s_db.text().strip(), base.description, self.s_params.rows(),
                     self.k_mod.isChecked(), base.approved_hash, base.approved_at, self.k_tx.isChecked(),
                     self.s_timeout.value(), self.k_save.isChecked(), self.s_dir.text().strip())
        return s

    def _update_scn_status(self) -> None:
        s = self._collect_scn()
        if not s.sql.strip():
            self.lbl_scn.setText("Введите текст SQL.")
            return
        ok, reason, a = check_permission(s)
        level = {RiskLevel.READ: "только чтение", RiskLevel.MODIFY: "изменяет данные",
                 RiskLevel.DANGEROUS: "потенциально опасный"}[a.level]
        self.lbl_scn.setText(f"Сценарий: {level}. " + ("Можно запускать." if ok else reason.split("\n")[0]))

    def _on_allow_mod(self, on: bool) -> None:
        if on and QMessageBox.warning(
                self, "Разрешить изменения", "Сценарий сможет изменять данные. Перед запуском текст SQL нужно будет "
                "подтвердить; после любой правки текста подтверждение снимается. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            self.k_mod.blockSignals(True)
            self.k_mod.setChecked(False)
            self.k_mod.blockSignals(False)
        self._update_scn_status()

    def new_scenario(self) -> None:
        s = Scenario.new("Новый сценарий", "SELECT 1 AS [Проверка]")
        s.profile_id = self.s_profile.currentData() or ""
        self.auto.scenarios.save(s)
        self.refresh_scenarios(s.id)

    def import_scenario(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт SQL", "", "SQL (*.sql);;Все файлы (*)")
        if not path:
            return
        try:
            s = self.auto.scenarios.import_sql_file(Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Импорт", str(exc))
            return
        QMessageBox.information(self, "Импорт", "Сценарий импортирован. Он не будет запускаться, пока вы не проверите "
                                                "текст и (для изменяющих скриптов) не подтвердите его.")
        self.refresh_scenarios(s.id)

    def delete_scenario(self) -> None:
        if self._scn and QMessageBox.question(self, "Удаление", f"Удалить сценарий «{self._scn.name}»?",
                                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                              QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.auto.scenarios.delete(self._scn.id)
            self.refresh_scenarios()

    def save_scenario(self) -> Scenario | None:
        s = self._collect_scn()
        self.auto.scenarios.save(s)
        self.refresh_scenarios(s.id)
        self.ctx.message.emit("Сценарий сохранён")
        return self.auto.scenarios.get(s.id)

    def approve_scenario(self) -> None:
        s = self.save_scenario()
        if s is None:
            return
        ok, _reason, a = check_permission(s)
        if a.is_read_only:
            QMessageBox.information(self, "Подтверждение", "Сценарий только читает данные — подтверждение не нужно.")
            return
        if not s.allow_modifications:
            QMessageBox.information(self, "Подтверждение", "Сначала отметьте «Разрешить изменения».")
            return
        word, okd = QInputDialog.getMultiLineText(
            self, "Подтверждение текста SQL", describe_risk(a) + f"\n\nПроверьте SQL. Для подтверждения введите {CONFIRM_WORD}:", "")
        if not okd or word.strip().upper() != CONFIRM_WORD:
            return
        approve(s)
        self.auto.scenarios.save(s)
        self.refresh_scenarios(s.id)

    def run_scenario_now(self) -> None:
        s = self.save_scenario()
        if s is None:
            return
        ok, reason, a = check_permission(s)
        if not ok:
            QMessageBox.warning(self, "Запуск сценария", reason)
            return
        if not a.is_read_only and QMessageBox.question(
                self, "Запуск", "Сценарий изменяет данные. Выполнить сейчас?\n\n" + describe_risk(a),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        profiles, store = self.svc.profiles, self.auto.scenarios

        def job(c):
            with profiles.lease(s.profile_id, "script") as conn:
                if s.database:
                    from database.editor_session import ExecutionListener
                    from database.sqltext import quote_ident
                    conn.execute_multi("USE " + quote_ident(s.database), listener=ExecutionListener())
                run = run_scenario(conn, s, {}, cancel_token=c.cancel_token)
            store.log_run(s.id, run)
            return run

        def done(run):
            self._load_scn(self.auto.scenarios.get(s.id))
            QMessageBox.information(self, "Сценарий", f"{STATUS_RU.get(run.status, run.status)}: {run.summary()}"
                                    + ("\n\n" + "\n".join(run.messages[:10]) if run.messages else ""))

        try:
            self.ctx.runner.submit(f"Сценарий: {s.name}", job, on_success=done,
                                   on_error=lambda m: QMessageBox.warning(self, "Сценарий", m), key=f"scenario:{s.id}")
        except DuplicateTaskError:
            QMessageBox.information(self, "Сценарий", "Сценарий уже выполняется.")

    # ------------------------------------------------------- резервные копии
    def _tab_backup(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.b_profile = profile_combo(self.ctx)
        self.b_db = QLineEdit()
        self.b_folder = QLineEdit()
        self.b_folder.setPlaceholderText("папка НА СЕРВЕРЕ, например D:\\Backup — файл пишет служба SQL Server")
        f.addRow("Подключение", self.b_profile)
        f.addRow("База данных", self.b_db)
        f.addRow("Папка на сервере", self.b_folder)
        lay.addLayout(f)
        opts = QHBoxLayout()
        self.b_copy = QCheckBox("COPY_ONLY")
        self.b_copy.setChecked(True)
        self.b_copy.setToolTip("Копия не влияет на цепочку разностных копий и журналов, настроенную администратором.")
        self.b_sum = QCheckBox("CHECKSUM")
        self.b_sum.setChecked(True)
        self.b_ver = QCheckBox("Проверить после создания")
        self.b_ver.setChecked(True)
        self.b_comp = QCheckBox("Сжатие")
        for wd in (self.b_copy, self.b_sum, self.b_ver, self.b_comp):
            opts.addWidget(wd)
        opts.addStretch(1)
        lay.addLayout(opts)
        row = QHBoxLayout()
        for text, fn in (("Создать копию сейчас", self.backup_now), ("История копий (msdb)", self.backup_history),
                         ("Инструкция по восстановлению", self.restore_help), ("Восстановить как новую базу…", self.restore_new)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f_=fn: f_())
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.t_bk = table(["База", "Окончание", "Тип", "COPY_ONLY", "Размер, МБ", "Файл"])
        self.t_bk.itemSelectionChanged.connect(self._on_backup_selected)
        lay.addWidget(self.t_bk, 1)
        cg = QGroupBox("Очистка старых копий (только файлы, созданные этой программой: База_ГГГГММДД_ччммсс.bak)")
        cf = QFormLayout(cg)
        self.c_local = QLineEdit()
        self.c_local.setPlaceholderText("папка с копиями, как она видна с этого компьютера")
        self.c_keep = QSpinBox()
        self.c_keep.setRange(1, 1000)
        self.c_keep.setValue(7)
        self.c_older = QSpinBox()
        self.c_older.setRange(0, 3650)
        self.c_older.setSpecialValueText("без ограничения")
        self.c_older.setSuffix(" дн.")
        b_plan = QPushButton("Показать, что будет удалено…")
        b_plan.clicked.connect(self.cleanup)
        cf.addRow("Папка", dir_row(self.c_local, self))
        cf.addRow("Хранить последних", self.c_keep)
        cf.addRow("Удалять старше", self.c_older)
        cf.addRow("", b_plan)
        lay.addWidget(cg)
        self.bk_log = QPlainTextEdit()
        self.bk_log.setReadOnly(True)
        self.bk_log.setMaximumHeight(130)
        lay.addWidget(self.bk_log)
        self._history: list[dict] = []
        return w

    def _bk_conn_args(self):
        return self.b_profile.currentData(), self.b_db.text().strip()

    def backup_now(self) -> None:
        pid, db = self._bk_conn_args()
        o = bk.BackupOptions(db, self.b_folder.text().strip(), self.b_copy.isChecked(), self.b_sum.isChecked(),
                             self.b_comp.isChecked(), self.b_ver.isChecked())
        errs = bk.validate_options(o)
        if not pid:
            errs.insert(0, "Выберите подключение.")
        if errs:
            QMessageBox.warning(self, "Резервное копирование", "\n".join(errs))
            return
        if QMessageBox.question(self, "Резервное копирование", f"Создать полную резервную копию базы «{db}»"
                                f"{' (COPY_ONLY)' if o.copy_only else ''} в папку сервера {o.folder}?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        profiles = self.svc.profiles

        def job(c):
            with profiles.lease(pid, "backup") as conn:
                return bk.create_backup(conn, o, cancel_token=c.cancel_token)

        def done(r):
            text = (f"{'Готово' if r.ok else 'Ошибка'}: {r.path}\n" + (r.error + "\n" if r.error else "") +
                    {True: "Копия проверена (RESTORE VERIFYONLY).", False: "Проверка не прошла.",
                     None: "Проверка не выполнялась."}[r.verified] + "\n" + "\n".join(r.notes + r.messages[-5:]))
            self.bk_log.setPlainText(text)
            self.svc.oplog.add("backup", f"Резервная копия {db}", "ok" if r.ok else "error", r.error or r.path)
            self.auto.notes.add("success" if r.ok else "error", f"Резервная копия «{db}»: {'готово' if r.ok else 'ошибка'}",
                                r.error or r.path, "backup")
            self.backup_history()

        self.bk_log.setPlainText("Создание копии…")
        try:
            self.ctx.runner.submit(f"Резервная копия: {db}", job, on_success=done,
                                   on_error=lambda m: self.bk_log.setPlainText("Ошибка: " + m), key=f"backup:{pid}:{db}")
        except DuplicateTaskError:
            pass

    def backup_history(self) -> None:
        pid, db = self._bk_conn_args()
        if not pid or not db:
            return
        profiles = self.svc.profiles

        def job(c):
            with profiles.lease(pid, "backup-history") as conn:
                return bk.history(conn, db, 50)

        def done(rows):
            self._history = rows
            fill(self.t_bk, [[r["database"], r["finished"], r["type"], "да" if r["copy_only"] else "нет",
                              f"{(r['size'] or 0) / 1048576:.1f}", r["path"]] for r in rows])

        self.ctx.runner.submit("История резервных копий", job, on_success=done,
                               on_error=lambda m: self.bk_log.setPlainText(m))

    def _on_backup_selected(self) -> None:
        r = self.t_bk.currentRow()
        if 0 <= r < len(self._history):
            self._selected_backup = self._history[r]["path"]

    def restore_help(self) -> None:
        _pid, db = self._bk_conn_args()
        path = getattr(self, "_selected_backup", "") or bk.join_server_path(self.b_folder.text() or "D:\\Backup",
                                                                             bk.backup_filename(db or "База", dt.datetime.now()))
        dlg = QDialog(self)
        dlg.setWindowTitle("Инструкция по восстановлению")
        dlg.resize(760, 500)
        l_ = QVBoxLayout(dlg)
        txt = QPlainTextEdit(bk.restore_instructions(db or "База", path))
        txt.setReadOnly(True)
        l_.addWidget(txt)
        b = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        b.rejected.connect(dlg.reject)
        l_.addWidget(b)
        dlg.exec()

    def restore_new(self) -> None:
        pid, _db = self._bk_conn_args()
        if not pid:
            return
        dlg = RestoreDialog(self, getattr(self, "_selected_backup", ""))
        if not dlg.exec():
            return
        path, name, confirm = dlg.e_path.text().strip(), dlg.e_name.text().strip(), dlg.e_confirm.text().strip()
        profiles = self.svc.profiles

        def job(c):
            with profiles.lease(pid, "restore") as conn:
                return bk.restore_as_new(conn, path, name, confirm, cancel_token=c.cancel_token)

        def done(msgs):
            self.bk_log.setPlainText(f"База «{name}» восстановлена из {path}.\n" + "\n".join(msgs[-5:]))
            self.svc.oplog.add("backup", f"Восстановление в новую базу {name}", "ok", path)

        self.ctx.runner.submit(f"Восстановление: {name}", job, on_success=done,
                               on_error=lambda m: QMessageBox.warning(self, "Восстановление", m), key=f"restore:{name}")

    def cleanup(self) -> None:
        _pid, db = self._bk_conn_args()
        folder = Path(self.c_local.text().strip()) if self.c_local.text().strip() else None
        if not db or folder is None:
            QMessageBox.information(self, "Очистка", "Укажите базу данных и папку с копиями.")
            return
        try:
            plan = bk.plan_cleanup(folder, db, keep_last=self.c_keep.value(), older_than_days=self.c_older.value())
        except bk.BackupError as exc:
            QMessageBox.warning(self, "Очистка", str(exc))
            return
        if not plan:
            QMessageBox.information(self, "Очистка", "Удалять нечего: все копии попадают под правило хранения.")
            return
        listing = "\n".join(p.name for p in plan[:30]) + (f"\n… и ещё {len(plan) - 30}" if len(plan) > 30 else "")
        if QMessageBox.warning(self, "Удаление старых копий", f"Будут безвозвратно удалены файлы ({len(plan)}):\n\n{listing}"
                               "\n\nУдалить?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        deleted, errors = bk.apply_cleanup(plan)
        self.bk_log.setPlainText(f"Удалено файлов: {len(deleted)}" + ("\nОшибки:\n" + "\n".join(errors) if errors else ""))
        self.svc.oplog.add("backup", f"Очистка копий {db}", "warning" if errors else "ok", f"удалено {len(deleted)}")

    # ----------------------------------------------------------- уведомления
    def _tab_notes(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        for text, fn in (("Отметить все прочитанными", self.mark_read), ("Очистить", self.clear_notes)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.t_notes = table(["Время", "Уровень", "Событие", "Подробности", "Источник"])
        lay.addWidget(self.t_notes, 1)
        return w

    def refresh_notes(self) -> None:
        self._notes = self.auto.notes.recent(200)
        fill(self.t_notes, [[n.ts.replace("T", " "), LEVEL_RU.get(n.level, n.level) + ("" if n.is_read else " •"),
                             n.title, n.message, n.source] for n in self._notes])

    def mark_read(self) -> None:
        self.auto.notes.mark_all_read()
        self.refresh_notes()
        self.ctx.notifications_changed.emit()

    def clear_notes(self) -> None:
        self.auto.notes.clear()
        self.refresh_notes()
        self.ctx.notifications_changed.emit()

    # ------------------------------------------------------------------ общее
    def _reload_profiles(self) -> None:
        for combo in (self.s_profile, self.b_profile):
            cur = combo.currentData()
            combo.clear()
            for p in self.svc.profiles.repo.list():
                combo.addItem(p.name, p.id)
            combo.setCurrentIndex(max(0, combo.findData(cur)))

    def refresh_all(self) -> None:
        self.refresh_tasks()
        self.refresh_scenarios()
        self.refresh_notes()

    def show_notifications(self) -> None:
        self.tabs.setCurrentIndex(3)
