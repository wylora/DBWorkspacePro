"""Сравнение баз: структура и данные, отчёты, скрипт синхронизации (просмотр и выполнение с подтверждением)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from comparison.data_compare import DataCompareRequest, DataCompareResult, TableRef
from comparison.report import data_diff_doc, schema_diff_doc
from comparison.schema_diff import CATEGORY_RU, STATUS_RU, SchemaDiff
from comparison.service import CompareService
from comparison.sync_script import SyncScript, sync_data_script, sync_schema_script
from database.sql_guard import analyze, describe as describe_risk
from reports.adhoc import save_doc
from utils.files import atomic_write_text
from utils.tasks import DuplicateTaskError

from .automation_page import fill, profile_combo, table
from .code_editor import CodeEditor
from .context import AppContext
from .helpers import open_path
from .widgets import PageHeader

FILTERS = [("Все различия", None), ("Только в исходной", "only_source"), ("Только в целевой", "only_target"),
           ("Различается", "changed"), ("Сравнить нельзя", "unknown")]


def split_table(text: str) -> tuple[str, str]:
    t = text.strip().replace("[", "").replace("]", "")
    return tuple(t.split(".", 1)) if "." in t else ("dbo", t)


class SyncScriptDialog(QDialog):
    """Показывает скрипт. Выполнение — только после ввода имени целевой базы."""

    def __init__(self, ctx: AppContext, parent, script: SyncScript, target_profile: str, target_db: str):
        super().__init__(parent)
        self.ctx, self.script, self.profile, self.db = ctx, script, target_profile, target_db
        self.setWindowTitle(f"Скрипт синхронизации → {target_db}")
        self.resize(980, 680)
        lay = QVBoxLayout(self)
        a = analyze(script.text)
        info = QLabel(f"Команд: {script.statements}. Оценка: {describe_risk(a).splitlines()[0] if script.statements else 'изменений нет'}")
        info.setWordWrap(True)
        lay.addWidget(info)
        if script.warnings or script.manual:
            w = QPlainTextEdit("\n".join(["ПРЕДУПРЕЖДЕНИЯ:"] + ["• " + x for x in script.warnings] +
                                         (["", "ВЫПОЛНИТЬ ВРУЧНУЮ:"] + ["• " + x for x in script.manual] if script.manual else [])))
            w.setReadOnly(True)
            w.setMaximumHeight(120)
            lay.addWidget(w)
        self.editor = CodeEditor(ctx.theme, ctx.services.settings.get("editor_font_size"))
        self.editor.setPlainText(script.text)
        self.editor.setReadOnly(True)
        lay.addWidget(self.editor, 1)
        row = QHBoxLayout()
        for text, fn in (("Сохранить .sql…", self._save), ("Открыть в SQL-редакторе", self._to_editor)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        self.b_run = QPushButton("Выполнить на целевой базе…")
        self.b_run.setEnabled(script.statements > 0)
        self.b_run.clicked.connect(self._run)
        row.addWidget(self.b_run)
        row.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)
        self.status = QLabel("Скрипт ещё не выполнялся. Он будет выполнен одной транзакцией: при ошибке изменения откатятся.")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить скрипт", f"sync_{self.db}.sql", "SQL (*.sql)")
        if path:
            atomic_write_text(Path(path), self.script.text)

    def _to_editor(self) -> None:
        self.ctx.open_sql.emit(self.script.text)
        self.accept()

    def _run(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Подтверждение", f"Скрипт изменит структуру или данные базы «{self.db}».\nРекомендуется сначала сделать "
            f"резервную копию.\n\nДля выполнения введите имя целевой базы данных:")
        if not ok or text.strip() != self.db:
            if ok:
                QMessageBox.warning(self, "Отменено", "Имя базы введено неверно — скрипт не выполнен.")
            return
        cs = CompareService(self.ctx.services)
        profile, db, sql = self.profile, self.db, self.script.text

        def job(c):
            return cs.execute_script(profile, db, sql, cancel_token=c.cancel_token, progress=lambda s, f: c.report(s, f))

        def done(r):
            if r.ok:
                self.status.setText(f"Выполнено успешно: пакетов {r.batches_done} из {r.batches_total}, {r.elapsed:.1f} с. "
                                    "Изменения зафиксированы.")
                self.b_run.setEnabled(False)
            else:
                self.status.setText(f"Ошибка: {r.error}. " + ("Все изменения скрипта откачены." if r.rolled_back else
                                                              "Откат не подтверждён — проверьте базу."))
            QMessageBox.information(self, "Синхронизация", self.status.text())

        self.status.setText("Выполнение…")
        self.b_run.setEnabled(False)
        try:
            self.ctx.runner.submit(f"Синхронизация → {db}", job, on_success=done,
                                   on_error=lambda m: self.status.setText("Ошибка: " + m), key=f"sync:{db}")
        except DuplicateTaskError:
            pass


class ComparePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self.cs = CompareService(ctx.services)
        self.diff: SchemaDiff | None = None
        self.data: DataCompareResult | None = None
        self._shown = []
        self._build()
        ctx.connections_changed.connect(self._reload_profiles)

    def _side(self, title: str, with_table: bool) -> tuple[QGroupBox, dict]:
        g = QGroupBox(title)
        f = QFormLayout(g)
        x = {"profile": profile_combo(self.ctx), "db": QLineEdit()}
        f.addRow("Подключение", x["profile"])
        f.addRow("База данных", x["db"])
        if with_table:
            x["table"] = QLineEdit()
            x["table"].setPlaceholderText("схема.таблица")
            f.addRow("Таблица", x["table"])
        return g, x

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Сравнение баз данных",
            "Сравнение структуры (таблицы, столбцы, ключи, индексы, представления, процедуры, функции, триггеры) и данных "
            "по ключу. Базы могут быть на разных серверах. Скрипт синхронизации только формируется; выполняется он "
            "лишь после просмотра и подтверждения."))
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---- структура
        w = QWidget()
        lay = QVBoxLayout(w)
        sides = QHBoxLayout()
        g1, self.s_src = self._side("Исходная база (образец)", False)
        g2, self.s_tgt = self._side("Целевая база (что приводим к образцу)", False)
        sides.addWidget(g1)
        sides.addWidget(g2)
        lay.addLayout(sides)
        opts = QHBoxLayout()
        self.e_tables = QLineEdit()
        self.e_tables.setPlaceholderText("необязательно: только эти таблицы (схема.таблица через запятую)")
        self.k_defs = QCheckBox("Сравнивать определения модулей")
        self.k_defs.setChecked(True)
        self.k_ws = QCheckBox("Игнорировать пробелы")
        self.k_ws.setChecked(True)
        self.k_coll = QCheckBox("Игнорировать сортировку (COLLATE)")
        self.k_names = QCheckBox("Сравнивать имена ограничений")
        for x in (self.e_tables, self.k_defs, self.k_ws, self.k_coll, self.k_names):
            opts.addWidget(x)
        lay.addLayout(opts)
        row = QHBoxLayout()
        self.b_cmp = QPushButton("Сравнить структуру")
        self.b_cmp.setObjectName("Primary")
        self.b_cmp.clicked.connect(self.compare_schema)
        self.c_filter = QComboBox()
        for text, val in FILTERS:
            self.c_filter.addItem(text, val)
        self.c_filter.currentIndexChanged.connect(lambda _i: self._show_diff())
        self.k_drops = QCheckBox("Включить в скрипт удаления (опасно)")
        row.addWidget(self.b_cmp)
        row.addWidget(QLabel("Показать:"))
        row.addWidget(self.c_filter)
        for text, fn in (("Отчёт Word…", lambda: self._report("docx")), ("Отчёт Excel…", lambda: self._report("xlsx")),
                         ("Скрипт синхронизации…", self.schema_script)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addWidget(self.k_drops)
        row.addStretch(1)
        lay.addLayout(row)
        self.lbl_schema = QLabel("Выберите две базы и нажмите «Сравнить структуру».")
        self.lbl_schema.setWordWrap(True)
        lay.addWidget(self.lbl_schema)
        self.t_diff = table(["Вид", "Объект", "Состояние", "Подробности"])
        lay.addWidget(self.t_diff, 1)
        self.tabs.addTab(w, "Структура")

        # ---- данные
        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        sides = QHBoxLayout()
        g3, self.d_src = self._side("Исходная таблица (образец)", True)
        g4, self.d_tgt = self._side("Целевая таблица", True)
        sides.addWidget(g3)
        sides.addWidget(g4)
        l2.addLayout(sides)
        f = QFormLayout()
        self.e_keys = QLineEdit()
        self.e_keys.setPlaceholderText("ключевые столбцы через запятую, например Id")
        self.e_cols = QLineEdit()
        self.e_cols.setPlaceholderText("необязательно: какие столбцы сравнивать (по умолчанию — все общие)")
        self.s_max = QSpinBox()
        self.s_max.setRange(1_000, 2_000_000)
        self.s_max.setSingleStep(10_000)
        self.s_max.setValue(self.svc.settings.get("compare_max_rows"))
        self.s_max.setSuffix(" строк")
        self.k_ci = QCheckBox("Текстовые ключи без учёта регистра")
        self.k_ci.setChecked(True)
        self.k_del = QCheckBox("Включить в скрипт DELETE (опасно)")
        f.addRow("Ключ", self.e_keys)
        f.addRow("Столбцы", self.e_cols)
        f.addRow("Не больше", self.s_max)
        f.addRow("", self.k_ci)
        l2.addLayout(f)
        row = QHBoxLayout()
        b = QPushButton("Сравнить данные")
        b.setObjectName("Primary")
        b.clicked.connect(self.compare_data)
        row.addWidget(b)
        for text, fn in (("Отчёт Word…", lambda: self._report("docx", data=True)),
                         ("Отчёт Excel…", lambda: self._report("xlsx", data=True)), ("Скрипт синхронизации…", self.data_script)):
            bb = QPushButton(text)
            bb.clicked.connect(lambda _=False, f_=fn: f_())
            row.addWidget(bb)
        row.addWidget(self.k_del)
        row.addStretch(1)
        l2.addLayout(row)
        self.lbl_data = QLabel("Укажите две таблицы и ключ.")
        self.lbl_data.setWordWrap(True)
        l2.addWidget(self.lbl_data)
        self.data_tabs = QTabWidget()
        self.t_added = table([])
        self.t_removed = table([])
        self.t_changed = table(["Ключ", "Столбец", "В целевой", "В исходной"])
        for t, title in ((self.t_added, "Нет в целевой"), (self.t_removed, "Нет в исходной"), (self.t_changed, "Различаются")):
            self.data_tabs.addTab(t, title)
        l2.addWidget(self.data_tabs, 1)
        self.tabs.addTab(w2, "Данные")

    def _reload_profiles(self) -> None:
        for side in (self.s_src, self.s_tgt, self.d_src, self.d_tgt):
            c = side["profile"]
            cur = c.currentData()
            c.clear()
            for p in self.svc.profiles.repo.list():
                c.addItem(p.name, p.id)
            c.setCurrentIndex(max(0, c.findData(cur)))

    def _pair(self, side) -> tuple[str, str]:
        return side["profile"].currentData() or "", side["db"].text().strip()

    # ---------------------------------------------------------------- структура
    def compare_schema(self) -> None:
        src, tgt = self._pair(self.s_src), self._pair(self.s_tgt)
        if not all(src) or not all(tgt):
            QMessageBox.information(self, "Сравнение", "Выберите подключение и базу данных для обеих сторон.")
            return
        tables = [t.strip() for t in self.e_tables.text().split(",") if t.strip()]
        opts = dict(compare_definitions=self.k_defs.isChecked(), ignore_whitespace=self.k_ws.isChecked(),
                    ignore_collation=self.k_coll.isChecked(), compare_names=self.k_names.isChecked())
        cs = self.cs

        def job(c):
            return cs.compare_schema(src, tgt, tables=tables or None, cancel_token=c.cancel_token,
                                     progress=lambda s, f: c.report(s, f), **opts)

        try:
            self.ctx.runner.submit("Сравнение структуры", job, on_success=self._schema_done,
                                   on_error=lambda m: QMessageBox.warning(self, "Сравнение", m), key="compare-schema")
        except DuplicateTaskError:
            return
        self.lbl_schema.setText("Чтение и сравнение структуры…")

    def _schema_done(self, d: SchemaDiff) -> None:
        self.diff = d
        self.lbl_schema.setText(d.summary() + f" Таблиц без различий: {d.equal_tables}, модулей без различий: "
                                f"{d.equal_modules}." + ("\n" + "\n".join(d.notes[:4]) if d.notes else ""))
        self._show_diff()

    def _show_diff(self) -> None:
        if self.diff is None:
            return
        st = self.c_filter.currentData()
        self._shown = [x for x in self.diff.items if st is None or x.status == st]
        fill(self.t_diff, [[CATEGORY_RU.get(x.category, x.category), x.object, STATUS_RU[x.status], "; ".join(x.details)]
                           for x in self._shown])

    def schema_script(self) -> None:
        if self.diff is None:
            QMessageBox.information(self, "Скрипт", "Сначала сравните структуру.")
            return
        s = sync_schema_script(self.diff, include_drops=self.k_drops.isChecked())
        pid, db = self._pair(self.s_tgt)
        SyncScriptDialog(self.ctx, self, s, pid, db).exec()

    # ------------------------------------------------------------------- данные
    def _ref(self, side) -> TableRef | None:
        pid, db = self._pair(side)
        tbl = side["table"].text().strip()
        if not (pid and db and tbl):
            return None
        schema, name = split_table(tbl)
        return TableRef(pid, db, schema, name)

    def compare_data(self) -> None:
        src, tgt = self._ref(self.d_src), self._ref(self.d_tgt)
        keys = [k.strip() for k in self.e_keys.text().split(",") if k.strip()]
        if src is None or tgt is None or not keys:
            QMessageBox.information(self, "Сравнение данных", "Укажите обе таблицы (подключение, база, таблица) и ключ.")
            return
        req = DataCompareRequest(src, tgt, keys, [c.strip() for c in self.e_cols.text().split(",") if c.strip()],
                                 self.s_max.value(), self.k_ci.isChecked())
        cs = self.cs

        def job(c):
            return cs.compare_data(req, cancel_token=c.cancel_token, progress=lambda s, f: c.report(s, f))

        try:
            self.ctx.runner.submit("Сравнение данных", job, on_success=self._data_done,
                                   on_error=lambda m: QMessageBox.warning(self, "Сравнение данных", m), key="compare-data")
        except DuplicateTaskError:
            return
        self.lbl_data.setText("Чтение и сравнение данных…")

    def _data_done(self, r: DataCompareResult) -> None:
        self.data = r
        d = r.diff
        self.lbl_data.setText(f"{d.summary()}. Строк: в исходной {r.source_rows}, в целевой {r.target_rows}."
                              + (" " + " ".join(r.notes) if r.notes else ""))
        for t, rows in ((self.t_added, d.added), (self.t_removed, d.removed)):
            t.setColumnCount(len(r.columns))
            t.setHorizontalHeaderLabels(r.columns)
            fill(t, [list(x) for x in rows])
        fill(self.t_changed, [[", ".join(map(str, ch.key)), col, old, new] for ch in d.changed
                              for col, (old, new) in ch.changes.items()])

    def data_script(self) -> None:
        if self.data is None:
            QMessageBox.information(self, "Скрипт", "Сначала сравните данные.")
            return
        r = self.data
        s = sync_data_script(r.request.target.schema, r.request.target.table, r.columns, r.request.key_columns, r.diff,
                             include_deletes=self.k_del.isChecked(), identity_columns=r.identity_columns)
        SyncScriptDialog(self.ctx, self, s, r.request.target.profile_id, r.request.target.database).exec()

    # ------------------------------------------------------------------- отчёты
    def _report(self, fmt: str, data: bool = False) -> None:
        if (self.data if data else self.diff) is None:
            QMessageBox.information(self, "Отчёт", "Сначала выполните сравнение.")
            return
        if data:
            doc = data_diff_doc(self.data)
            stem = f"Сравнение_данных_{self.data.request.target.table}"
        else:
            doc = schema_diff_doc(self.diff, f"{self.diff.source.server}/{self.diff.source.database}",
                                  f"{self.diff.target.server}/{self.diff.target.database}")
            stem = f"Сравнение_структуры_{self.diff.source.database}_{self.diff.target.database}"
        folder = QFileDialog.getExistingDirectory(self, "Папка для отчёта", str(self.ctx.reports.output_dir()))
        if not folder:
            return
        res = save_doc(doc, Path(folder), stem, [fmt], temp_root=self.svc.paths.temp)
        if res.errors:
            QMessageBox.warning(self, "Отчёт", "\n".join(res.errors.values()))
        elif res.files:
            open_path(str(res.files[0]))
