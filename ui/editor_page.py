"""Страница «SQL-редактор»: вкладки скриптов, выполнение в фоне, результаты, транзакции, экспорт."""
from __future__ import annotations

import itertools
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableView, QTabWidget,
    QVBoxLayout, QWidget,
)

from database.completion import complete
from database.connection import ConnectionState
from database.editor_session import EditorSession, ExecutionListener, ExecutionReport
from database.errors import DbWorkspaceError, friendly_message
from database.metadata import MetadataReader
from database.sql_diagnostics import find_syntax_issues, locate_error
from database.sql_guard import Analysis, RiskLevel, analyze, describe
from utils.export import ExportError, export_rows, rows_to_tsv
from utils.files import read_text_auto, write_sql_file
from utils.tasks import DuplicateTaskError, TaskCancelled

from .code_editor import CodeEditor
from .context import AppContext
from .export_dialog import ExportDialog
from .query_dialogs import FavoritesDialog, HistoryDialog
from .result_model import ResultTableModel
from .widgets import EmptyState, PageHeader

log = logging.getLogger("dbworkspace.editor")
CONFIRM_WORD = "ВЫПОЛНИТЬ"
_uid = itertools.count(1)


class TabSignals(QObject):
    """Сигналы вкладки: излучаются из рабочего потока, доставляются в GUI-поток."""
    result_started = Signal(int, object)
    rows_ready = Signal(int, object)
    result_done = Signal(int, int, bool)
    count_ready = Signal(int, int)
    messages_ready = Signal(object)


class QtListener(ExecutionListener):
    def __init__(self, signals: TabSignals):
        self.s = signals

    def on_result(self, index, columns):
        self.s.result_started.emit(index, list(columns))
        return None

    def on_rows(self, index, rows):
        self.s.rows_ready.emit(index, rows)

    def on_result_done(self, index, count, truncated):
        self.s.result_done.emit(index, count, truncated)

    def on_rowcount(self, index, affected):
        self.s.count_ready.emit(index, affected)

    def on_messages(self, messages):
        self.s.messages_ready.emit(list(messages))


class ResultMeta:
    def __init__(self, view: QTableView, model: ResultTableModel, number: int):
        self.view, self.model, self.number = view, model, number
        self.truncated = False
        self.count = 0


class EditorTab(QWidget):
    def __init__(self, ctx: AppContext, profile_id: str | None, theme: str, font_size: int, parent=None):
        super().__init__(parent)
        self.uid = next(_uid)
        self.ctx = ctx
        self.profile_id = profile_id
        self.database: str | None = None
        self.allow_mod = False
        self.tx = False
        self.timeout = 0
        self.session: EditorSession | None = None
        self.task = None
        self.path: Path | None = None
        self.last_sql = ""
        self.last_offset = 0
        self.metas: dict[int, ResultMeta] = {}
        self._rowset_counter = 0

        self.signals = TabSignals(self)
        self.listener = QtListener(self.signals)
        self.signals.result_started.connect(self._on_result_started)
        self.signals.rows_ready.connect(self._on_rows)
        self.signals.result_done.connect(self._on_result_done)
        self.signals.count_ready.connect(self._on_count)
        self.signals.messages_ready.connect(self._on_messages)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Vertical)
        self.editor = CodeEditor(theme, font_size)
        split.addWidget(self.editor)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 4, 0, 0)
        bl.setSpacing(2)
        self.results = QTabWidget()
        self.results.setDocumentMode(True)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self.results.addTab(self.messages, "Сообщения")
        bl.addWidget(self.results, 1)
        self.status = QLabel("Готово")
        self.status.setObjectName("Muted")
        bl.addWidget(self.status)
        split.addWidget(bottom)
        split.setSizes([320, 280])
        lay.addWidget(split)

    # ---- состояние ----------------------------------------------------
    @property
    def running(self) -> bool:
        return self.task is not None and self.task.is_active

    @property
    def in_transaction(self) -> bool:
        return bool(self.session and self.session.in_transaction)

    def title(self) -> str:
        name = self.path.name if self.path else f"Запрос {self.uid}"
        return ("* " if self.editor.document().isModified() else "") + name + (" ▶" if self.running else "")

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("color: #D32F2F;" if error else "")

    def say(self, text: str) -> None:
        self.messages.appendPlainText(text)

    # ---- результаты ---------------------------------------------------
    def reset_results(self) -> None:
        while self.results.count() > 1:
            w = self.results.widget(0)
            self.results.removeTab(0)
            w.deleteLater()
        self.metas.clear()
        self._rowset_counter = 0
        self.messages.clear()

    def _on_result_started(self, index: int, columns) -> None:
        self._rowset_counter += 1
        model = ResultTableModel(columns, self)
        view = QTableView()
        view.setModel(model)
        view.setAlternatingRowColors(True)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.verticalHeader().setDefaultSectionSize(24)
        view.horizontalHeader().setStretchLastSection(False)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        meta = ResultMeta(view, model, self._rowset_counter)
        self.metas[index] = meta
        view.customContextMenuRequested.connect(lambda pos, m=meta: self.result_menu(m, pos))
        copy_sc = QShortcut(QKeySequence("Ctrl+C"), view)
        copy_sc.setContext(Qt.ShortcutContext.WidgetShortcut)      # иначе несколько таблиц конфликтуют
        copy_sc.activated.connect(lambda m=meta: self.copy_selection(m, False))
        self.results.insertTab(self.results.count() - 1, view, f"Результат {meta.number}")
        if meta.number == 1:
            self.results.setCurrentWidget(view)

    def _on_rows(self, index: int, rows) -> None:
        meta = self.metas.get(index)
        if meta is None:
            return
        first = meta.model.rowCount() == 0
        meta.model.append_rows(rows)
        if first:
            meta.view.resizeColumnsToContents()
            for c in range(meta.model.columnCount()):
                if meta.view.columnWidth(c) > 420:
                    meta.view.setColumnWidth(c, 420)
        self._title(meta)

    def _on_result_done(self, index: int, count: int, truncated: bool) -> None:
        meta = self.metas.get(index)
        if meta is None:
            return
        meta.count, meta.truncated = count, truncated
        self._title(meta)
        meta.view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        meta.view.setSortingEnabled(True)

    def _title(self, meta: ResultMeta) -> None:
        n = meta.model.rowCount()
        idx = self.results.indexOf(meta.view)
        if idx >= 0:
            self.results.setTabText(idx, f"Результат {meta.number} ({n}{'+' if meta.truncated else ''})")

    def _on_count(self, index: int, affected: int) -> None:
        self.say(f"(затронуто строк: {affected})")

    def _on_messages(self, messages) -> None:
        for m in messages:
            self.say(m)

    def selected_meta(self) -> ResultMeta | None:
        w = self.results.currentWidget()
        return next((m for m in self.metas.values() if m.view is w), None)

    # ---- копирование и меню -------------------------------------------
    def copy_selection(self, meta: ResultMeta, header: bool) -> None:
        idxs = meta.view.selectionModel().selectedIndexes()
        if not idxs:
            return
        rows = sorted({i.row() for i in idxs})
        cols = sorted({i.column() for i in idxs})
        chosen = {(i.row(), i.column()) for i in idxs}
        data = [tuple(meta.model.rows[r][c] if (r, c) in chosen else None for c in cols) for r in rows]
        QApplication.clipboard().setText(rows_to_tsv([meta.model.columns[c] for c in cols], data, header))
        self.ctx.message.emit(f"Скопировано ячеек: {len(chosen)}")

    def result_menu(self, meta: ResultMeta, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Копировать", lambda: self.copy_selection(meta, False))
        menu.addAction("Копировать с заголовками", lambda: self.copy_selection(meta, True))
        menu.addAction("Выделить всё", meta.view.selectAll)
        menu.addSeparator()
        menu.addAction("Экспорт этого результата…", lambda: self.ctx.navigate.emit("__export__"))
        menu.exec(meta.view.viewport().mapToGlobal(pos))


class EditorPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self._building = False
        self._db_cache: dict[str, list[str]] = {}
        self._build()
        ctx.connections_changed.connect(self._reload_connections)
        ctx.profiles_changed.connect(self._reload_connections)
        ctx.theme_changed.connect(self._apply_theme)
        ctx.settings_changed.connect(self._apply_font)
        ctx.runner.task_changed.connect(lambda _t: self._sync_toolbar())
        self._reload_connections()
        self.new_tab()

    # ---- построение ---------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.setSpacing(8)
        root.addWidget(PageHeader(
            "SQL-редактор",
            "Каждая вкладка работает на собственном соединении. По умолчанию включён режим «только чтение»: "
            "изменяющие команды блокируются, пока вы явно не разрешите их и не подтвердите выполнение."))

        bar = QFrame()
        bar.setObjectName("Card")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(6)
        r1, r2 = QHBoxLayout(), QHBoxLayout()
        bl.addLayout(r1)
        bl.addLayout(r2)

        self.c_conn = QComboBox()
        self.c_conn.setMinimumWidth(190)
        self.c_conn.setToolTip("Активное подключение для этой вкладки")
        self.c_conn.activated.connect(self._on_conn_changed)
        self.c_db = QComboBox()
        self.c_db.setMinimumWidth(160)
        self.c_db.setToolTip("База данных (USE) для этой вкладки")
        self.c_db.activated.connect(self._on_db_changed)
        self.b_run = QPushButton("▶ Выполнить (F5)")
        self.b_run.setObjectName("Primary")
        self.b_run.clicked.connect(self.run)
        self.b_cancel = QPushButton("■ Отмена")
        self.b_cancel.clicked.connect(self.cancel)
        self.k_mod = QCheckBox("Разрешить изменения")
        self.k_mod.setToolTip("Выключено: INSERT/UPDATE/DELETE/DDL/EXEC и т. п. блокируются.\n"
                              "Даже при включении потребуется подтверждение.")
        self.k_mod.toggled.connect(self._on_mod_toggled)
        self.k_tx = QCheckBox("Транзакция")
        self.k_tx.setToolTip("Команды выполняются в транзакции; изменения применяются только после «Зафиксировать».")
        self.k_tx.toggled.connect(self._on_tx_toggled)
        self.b_commit = QPushButton("Зафиксировать")
        self.b_commit.clicked.connect(lambda: self._finish_tx(True))
        self.b_rollback = QPushButton("Откатить")
        self.b_rollback.clicked.connect(lambda: self._finish_tx(False))
        self.s_timeout = QSpinBox()
        self.s_timeout.setRange(0, 86400)
        self.s_timeout.setSuffix(" с")
        self.s_timeout.setSpecialValueText("тайм-аут: профиль")
        self.s_timeout.setToolTip("Тайм-аут запроса для этой вкладки. 0 — значение из профиля подключения.")
        self.s_timeout.editingFinished.connect(self._on_timeout_changed)
        for w in (QLabel("Подключение:"), self.c_conn, QLabel("База:"), self.c_db, self.b_run, self.b_cancel):
            r1.addWidget(w)
        r1.addStretch(1)
        for w in (self.k_mod, self.k_tx, self.b_commit, self.b_rollback, self.s_timeout):
            r2.addWidget(w)
        r2.addStretch(1)
        for text, fn, tip in (("Новая", lambda: self.new_tab(), "Новая вкладка (Ctrl+T)"),
                              ("Открыть…", self.open_file, "Открыть .sql (Ctrl+O). Скрипт не выполняется автоматически."),
                              ("Сохранить", lambda: self.save_file(False), "Сохранить (Ctrl+S)"),
                              ("Сохранить как…", lambda: self.save_file(True), ""),
                              ("История", self.show_history, "История выполненных запросов"),
                              ("Избранное", self.show_favorites, "Избранные запросы"),
                              ("★ В избранное", self.add_favorite, "Добавить текущий запрос в избранное"),
                              ("Экспорт…", self.export_result, "Экспорт текущего результата в CSV, Excel или JSON")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, f=fn: f())
            r2.addWidget(b)
        root.addWidget(bar)

        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab_index)
        self.tabs.currentChanged.connect(lambda _i: self._sync_toolbar(load_db=True))
        hl.addWidget(self.tabs)
        self.empty = EmptyState("Нет активных подключений",
                                "Подключитесь к SQL Server на странице «Подключения», затем возвращайтесь сюда.",
                                "Перейти к подключениям")
        self.empty.action_clicked.connect(lambda: self.ctx.navigate.emit("connections"))
        hl.addWidget(self.empty)
        root.addWidget(holder, 1)

        for seq, fn in (("F5", self.run), ("Ctrl+Return", self.run), ("Ctrl+S", lambda: self.save_file(False)),
                        ("Ctrl+O", self.open_file), ("Ctrl+T", lambda: self.new_tab()),
                        ("Ctrl+W", lambda: self._close_tab_index(self.tabs.currentIndex()))):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self.ctx.navigate.connect(self._on_navigate)

    def _on_navigate(self, key: str) -> None:
        if key == "__export__":
            self.export_result()

    # ---- вкладки -------------------------------------------------------
    def tab(self) -> EditorTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, EditorTab) else None

    def _all_tabs(self) -> list[EditorTab]:
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def new_tab(self, sql: str = "", path: Path | None = None) -> EditorTab:
        s = self.svc.settings
        tab = EditorTab(self.ctx, self.c_conn.currentData(), self.ctx.theme, s.get("editor_font_size"))
        tab.path = path
        tab.editor.setPlainText(sql)
        tab.editor.document().setModified(False)
        tab.editor.completion_provider = lambda text, pos, t=tab: self._complete(t, text, pos)
        tab.editor.document().modificationChanged.connect(lambda _m, t=tab: self._retitle(t))
        idx = self.tabs.addTab(tab, tab.title())
        self.tabs.setCurrentIndex(idx)
        tab.editor.setFocus()
        return tab

    def _retitle(self, tab: EditorTab) -> None:
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabText(i, tab.title())

    def _close_tab_index(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if isinstance(tab, EditorTab) and self._release_tab(tab):
            self.tabs.removeTab(index)
            tab.deleteLater()
            if self.tabs.count() == 0:
                self.new_tab()

    def _release_tab(self, tab: EditorTab) -> bool:
        """Спрашивает о несохранённом/выполняющемся и закрывает сеанс. False — пользователь отказался."""
        if tab.running:
            a = QMessageBox.question(self, "Запрос выполняется", "Отменить выполнение и закрыть вкладку?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
            if a != QMessageBox.StandardButton.Yes:
                return False
            self.ctx.services.tasks.cancel(tab.task.id)
            tab.task.wait(5)
        if tab.editor.document().isModified() and tab.editor.toPlainText().strip():
            box = QMessageBox(QMessageBox.Icon.Question, "Несохранённые изменения",
                              f"Сохранить изменения в «{tab.title().lstrip('* ')}»?", parent=self)
            save = box.addButton("Сохранить", QMessageBox.ButtonRole.AcceptRole)
            discard = box.addButton("Не сохранять", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is save:
                if not self._save_tab(tab, False):
                    return False
            elif box.clickedButton() is not discard:
                return False
        if tab.in_transaction:
            a = QMessageBox.question(
                self, "Открытая транзакция", "В этой вкладке есть незавершённая транзакция.\n"
                "Закрытие откатит все её изменения. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if a != QMessageBox.StandardButton.Yes:
                return False
        self._drop_session(tab)
        return True

    def _drop_session(self, tab: EditorTab) -> None:
        session, tab.session = tab.session, None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка закрытия сеанса редактора")

    def prepare_close(self) -> bool:
        """Вызывается при выходе из приложения: спрашивает о несохранённом и закрывает сеансы."""
        for tab in self._all_tabs():
            self.tabs.setCurrentWidget(tab)
            if not self._release_tab(tab):
                return False
        return True

    # ---- панель инструментов -------------------------------------------
    def _reload_connections(self) -> None:
        current = self.c_conn.currentData()
        self.c_conn.blockSignals(True)
        self.c_conn.clear()
        for p in self.svc.profiles.repo.list():
            conn = self.svc.manager.primary(p.id)
            if conn is not None and conn.state in (ConnectionState.CONNECTED, ConnectionState.LOST):
                self.c_conn.addItem(p.name, p.id)
        idx = self.c_conn.findData(current)
        self.c_conn.setCurrentIndex(idx if idx >= 0 else 0)
        self.c_conn.blockSignals(False)
        has = self.c_conn.count() > 0
        active = {self.c_conn.itemData(i) for i in range(self.c_conn.count())}
        for pid in [k for k in self._db_cache if k not in active]:
            del self._db_cache[pid]                 # подключение закрыто — список баз устарел
        self.tabs.setVisible(has)
        self.empty.setVisible(not has)
        tab = self.tab()
        if tab is not None and (tab.profile_id is None or self.c_conn.findData(tab.profile_id) < 0) and has \
                and tab.session is None:
            tab.profile_id = self.c_conn.currentData()
            tab.database = None
        self._sync_toolbar(load_db=True)

    def _sync_toolbar(self, load_db: bool = False) -> None:
        tab = self.tab()
        has_conn = self.c_conn.count() > 0
        busy = bool(tab and tab.running)
        self._building = True
        if tab is not None:
            i = self.c_conn.findData(tab.profile_id)
            if i >= 0:
                self.c_conn.setCurrentIndex(i)
            self.k_mod.setChecked(tab.allow_mod)
            self.k_tx.setChecked(tab.tx)
            self.s_timeout.setValue(tab.timeout)
        self._building = False
        self.b_run.setEnabled(bool(tab) and has_conn and not busy)
        self.b_cancel.setEnabled(busy)
        in_tx = bool(tab and tab.in_transaction)
        self.b_commit.setEnabled(bool(tab and tab.tx and in_tx and not busy))
        self.b_rollback.setEnabled(bool(tab and tab.tx and in_tx and not busy))
        for w in (self.c_conn, self.c_db, self.k_mod, self.k_tx, self.s_timeout):
            w.setEnabled(bool(tab) and not busy)
        if tab is not None:
            self._retitle(tab)
            if in_tx:
                tab.set_status("● Открыта транзакция: изменения не применены, пока вы не нажмёте «Зафиксировать»")
        if load_db and tab is not None and has_conn:
            self._load_databases(tab)

    def _on_conn_changed(self, _i) -> None:
        tab = self.tab()
        if tab is None or self._building:
            return
        new_id = self.c_conn.currentData()
        if new_id == tab.profile_id:
            return
        if tab.in_transaction:
            QMessageBox.information(self, "Открытая транзакция",
                                    "Сначала зафиксируйте или откатите транзакцию в этой вкладке.")
            self._sync_toolbar()
            return
        self._drop_session(tab)
        tab.profile_id, tab.database = new_id, None
        self._load_databases(tab)

    def _on_db_changed(self, _i) -> None:
        tab = self.tab()
        if tab is None or self._building:
            return
        tab.database = self.c_db.currentData()
        if tab.session is not None:
            tab.session.configure(database=tab.database or "")
        if tab.database:
            self._load_schema(tab)

    def _on_mod_toggled(self, on: bool) -> None:
        tab = self.tab()
        if tab is None or self._building:
            return
        if on:
            a = QMessageBox.warning(
                self, "Разрешить изменения?",
                "Режим «только чтение» будет выключен для этой вкладки.\n\nИзменяющие команды (INSERT, UPDATE, "
                "DELETE, DDL, EXEC…) станут доступны, но каждое выполнение потребует подтверждения.\n\n"
                "Рекомендуется включить «Транзакцию», чтобы можно было откатить изменения.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if a != QMessageBox.StandardButton.Yes:
                self._building = True
                self.k_mod.setChecked(False)
                self._building = False
                return
        tab.allow_mod = on
        if tab.session is not None:
            tab.session.allow_modifications = on

    def _on_tx_toggled(self, on: bool) -> None:
        tab = self.tab()
        if tab is None or self._building:
            return
        if not on and tab.in_transaction:
            QMessageBox.information(self, "Открытая транзакция",
                                    "Сначала зафиксируйте или откатите транзакцию.")
            self._building = True
            self.k_tx.setChecked(True)
            self._building = False
            return
        tab.tx = on
        self._sync_toolbar()

    def _on_timeout_changed(self) -> None:
        tab = self.tab()
        if tab is not None and not self._building:
            tab.timeout = self.s_timeout.value()

    def _apply_theme(self, name: str) -> None:
        for t in self._all_tabs():
            t.editor.set_theme(name)

    def _apply_font(self) -> None:
        size = self.svc.settings.get("editor_font_size")
        for t in self._all_tabs():
            t.editor.set_font_size(size)

    # ---- базы данных и автодополнение -----------------------------------
    def _primary(self, tab: EditorTab):
        return self.svc.manager.primary(tab.profile_id) if tab.profile_id else None

    def _fill_databases(self, tab: EditorTab, conn, names: list[str]) -> None:
        self.c_db.blockSignals(True)
        self.c_db.clear()
        self.c_db.addItem("(по умолчанию для логина)", None)
        for n in names:
            self.c_db.addItem(n, n)
        want = tab.database or conn.profile.database
        i = self.c_db.findData(want) if want else 0
        self.c_db.setCurrentIndex(max(0, i))
        self.c_db.blockSignals(False)
        tab.database = self.c_db.currentData()
        if tab.database and self.svc.schema_cache.get(tab.profile_id, tab.database) is None:
            self._load_schema(tab)

    def _load_databases(self, tab: EditorTab) -> None:
        conn = self._primary(tab)
        self.c_db.blockSignals(True)
        self.c_db.clear()
        self.c_db.addItem("(по умолчанию для логина)", None)
        self.c_db.blockSignals(False)
        if conn is None:
            return
        pid, batch = tab.profile_id, self.svc.settings.get("fetch_batch_size")
        if pid in self._db_cache:
            self._fill_databases(tab, conn, self._db_cache[pid])
            return

        def done(names):
            self._db_cache[pid] = names
            if self.tab() is tab and tab.profile_id == pid:
                self._fill_databases(tab, conn, names)

        try:
            self.ctx.runner.submit("Редактор: список баз",
                                   lambda c: MetadataReader(conn, batch_size=batch).list_databases(),
                                   on_success=done, on_error=lambda m: self.ctx.message.emit(m),
                                   key=f"editor-dbs:{pid}")
        except DuplicateTaskError:
            pass

    def _load_schema(self, tab: EditorTab) -> None:
        conn, db, pid = self._primary(tab), tab.database, tab.profile_id
        if conn is None or not db:
            return
        batch = self.svc.settings.get("fetch_batch_size")

        def job(ctx_):
            return self.svc.schema_cache.load(MetadataReader(conn, batch_size=batch), pid, db)

        def done(snap):
            note = " (список столбцов усечён)" if snap.truncated else ""
            self.ctx.message.emit(f"Автодополнение загружено для «{db}»: объектов {len(snap.objects)}{note}")

        try:
            self.ctx.runner.submit(f"Автодополнение: {db}", job, on_success=done,
                                   on_error=lambda m: self.ctx.message.emit("Автодополнение недоступно: " + m),
                                   key=f"editor-schema:{pid}:{db}")
        except DuplicateTaskError:
            pass

    def _complete(self, tab: EditorTab, text: str, pos: int):
        snap = self.svc.schema_cache.get(tab.profile_id, tab.database) if tab.profile_id and tab.database else None
        return complete(text, pos, snap)

    # ---- выполнение ----------------------------------------------------
    def run(self) -> None:
        tab = self.tab()
        if tab is None or tab.running:
            return
        sql, offset = tab.editor.sql_to_run()
        if not sql.strip():
            self.ctx.message.emit("Нечего выполнять: введите запрос.")
            return
        tab.editor.clear_errors()
        issues = find_syntax_issues(sql)
        if issues:
            tab.editor.set_errors([(i.start + offset, i.end + offset, i.message) for i in issues])
            tab.editor.goto_offset(issues[0].start + offset)
            tab.set_status("Синтаксис: " + issues[0].message, error=True)
            tab.say("Синтаксические проблемы (выполнение не начато):\n" + "\n".join(f"  • {i.message}" for i in issues))
            tab.results.setCurrentWidget(tab.messages)
            return
        analysis = analyze(sql)
        if analysis.errors:
            tab.set_status(analysis.errors[0], error=True)
            tab.say("\n".join(analysis.errors))
            return
        if not tab.profile_id or self._primary(tab) is None:
            QMessageBox.information(self, "Нет подключения",
                                    "Подключитесь к серверу на странице «Подключения».")
            return
        confirmed = False
        if analysis.level > RiskLevel.READ:
            if not tab.allow_mod:
                QMessageBox.information(
                    self, "Режим «только чтение»",
                    "Скрипт содержит команды, которые изменяют данные или схему, а режим «только чтение» "
                    "включён. Чтобы выполнить его, отметьте «Разрешить изменения».\n\n" + describe(analysis))
                return
            if not self._confirm(analysis, tab):
                return
            confirmed = True
        self._start(tab, sql, offset, confirmed)

    def _confirm(self, analysis: Analysis, tab: EditorTab) -> bool:
        text = describe(analysis)
        if analysis.level is RiskLevel.DANGEROUS and not tab.tx:
            word, ok = QInputDialog.getMultiLineText(
                self, "Подтверждение опасной операции",
                f"{text}\n\nОперация может безвозвратно изменить или удалить данные. "
                f"Для продолжения введите слово {CONFIRM_WORD}:", "")
            return bool(ok and word.strip().upper() == CONFIRM_WORD)
        extra = "\n\nВыполнение идёт в транзакции: изменения можно откатить." if tab.tx else ""
        a = QMessageBox.question(
            self, "Подтверждение", f"Выполнить изменяющий скрипт?\n\n{text}{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return a == QMessageBox.StandardButton.Yes

    def _ensure_session(self, tab: EditorTab) -> EditorSession:
        if tab.session is not None and tab.session.conn.state is not ConnectionState.DISCONNECTED:
            return tab.session
        mgr, pid, s = self.svc.manager, tab.profile_id, self.svc.settings
        conn = mgr.open_session(pid)
        session = EditorSession(conn, release=mgr.release, sibling_factory=lambda: mgr.open_session(pid),
                                preview_limit=s.get("preview_row_limit"), batch_size=s.get("fetch_batch_size"))
        tab.session = session
        return session

    def _start(self, tab: EditorTab, sql: str, offset: int, confirmed: bool) -> None:
        tab.reset_results()
        tab.last_sql, tab.last_offset = sql, offset
        tab.set_status("Выполняется…")
        allow, db, tx = tab.allow_mod, tab.database, tab.tx
        timeout = tab.timeout or None

        def job(ctx_):
            session = self._ensure_session(tab)
            session.allow_modifications = allow
            session.preview_limit = self.svc.settings.get("preview_row_limit")
            session.configure(database=db or "", transaction=tx, timeout=timeout)
            report = session.execute(sql, confirmed=confirmed, cancel_token=ctx_.cancel_token,
                                     listener=tab.listener)
            if report.cancelled:
                raise TaskCancelled()
            return report

        def hook():
            if tab.session is not None:
                tab.session.cancel()

        def done(report: ExecutionReport):
            tab.task = None
            self._finished(tab, report, sql, offset)

        def failed(msg: str):
            tab.task = None
            tab.set_status(msg, error=True)
            tab.say("Ошибка: " + msg)
            tab.results.setCurrentWidget(tab.messages)
            self._history(tab, sql, "error", None, 0, msg)
            self._sync_toolbar()

        def cancelled():
            tab.task = None
            tab.set_status("Выполнение отменено.")
            tab.say("Выполнение отменено пользователем. Уже полученные строки сохранены в результатах.")
            self._history(tab, sql, "cancelled", None, 0, "отменён")
            self._sync_toolbar()

        try:
            tab.task = self.ctx.runner.submit(f"SQL: {' '.join(sql.split())[:50]}", job, on_success=done,
                                              on_error=failed, on_cancelled=cancelled,
                                              key=f"sql:{tab.uid}", on_cancel=hook)
        except DuplicateTaskError:
            return
        self._sync_toolbar()

    def _finished(self, tab: EditorTab, report: ExecutionReport, sql: str, offset: int) -> None:
        results = [r for r in report.results if r.kind == "rows"]
        parts = [f"{report.elapsed:.2f} с"]
        if results:
            parts.append(f"наборов: {len(results)}")
            parts.append(f"строк: {sum(r.row_count for r in results)}")
        if any(r.truncated for r in results):
            parts.append("показаны не все строки (ограничение предпросмотра)")
        if report.batches_total > 1:
            parts.append(f"пакетов: {report.batches_done}/{report.batches_total}")
        if report.error:
            tab.set_status("Ошибка · " + " · ".join(parts), error=True)
            tab.say("Ошибка: " + report.error)
            rng = locate_error(sql, report.error)
            if rng:
                tab.editor.set_errors([(rng[0] + offset, rng[1] + offset, report.error)])
                tab.editor.goto_offset(rng[0] + offset)
                tab.say("(место ошибки определено приблизительно по тексту сообщения сервера)")
            tab.results.setCurrentWidget(tab.messages)
        else:
            tab.set_status("Выполнено · " + " · ".join(parts))
            if not results:
                if not report.messages:
                    tab.say("Команды выполнены успешно.")
                tab.results.setCurrentWidget(tab.messages)
        if tab.session and tab.session.database and tab.session.database != tab.database:
            tab.database = tab.session.database          # в скрипте был USE
            i = self.c_db.findData(tab.database)
            if i >= 0:
                self.c_db.setCurrentIndex(i)
        self._history(tab, sql, "error" if report.error else "ok", int(report.elapsed * 1000),
                      report.total_rows, report.error or "")
        self._sync_toolbar()

    def _history(self, tab: EditorTab, sql: str, status: str, ms, rows: int, message: str) -> None:
        if not self.svc.settings.get("query_history_enabled"):
            return
        conn = self._primary(tab)
        try:
            self.svc.history.add(sql, status, connection_name=conn.profile.name if conn else "",
                                 database_name=tab.database or "", duration_ms=ms, row_count=rows, message=message)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось записать историю запросов")

    def cancel(self) -> None:
        tab = self.tab()
        if tab is None or not tab.running:
            return
        self.svc.tasks.cancel(tab.task.id)
        tab.set_status("Отмена… (если драйвер не поддерживает прерывание запроса, ожидание завершится само)")

    # ---- транзакции ----------------------------------------------------
    def _finish_tx(self, commit: bool) -> None:
        tab = self.tab()
        if tab is None or tab.session is None or not tab.in_transaction:
            return
        session = tab.session
        name = "Фиксация транзакции" if commit else "Откат транзакции"

        def job(ctx_):
            session.commit() if commit else session.rollback()

        def done(_r):
            tab.set_status("Транзакция зафиксирована." if commit else "Транзакция откачена.")
            tab.say(tab.status.text())
            self._sync_toolbar()

        def failed(msg):
            tab.set_status(msg, error=True)
            tab.say("Ошибка: " + msg)
            self._sync_toolbar()

        try:
            self.ctx.runner.submit(name, job, on_success=done, on_error=failed, key=f"sqltx:{tab.uid}")
        except DuplicateTaskError:
            pass

    # ---- файлы, история, избранное --------------------------------------
    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Открыть скрипт", "", "SQL (*.sql);;Все файлы (*)")
        if not path:
            return
        try:
            text, enc = read_text_auto(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось открыть файл", str(exc))
            return
        tab = self.tab()
        if tab is not None and not tab.editor.toPlainText() and not tab.path and not tab.editor.document().isModified():
            tab.editor.setPlainText(text)
            tab.path = Path(path)
            tab.editor.document().setModified(False)
            self._retitle(tab)
        else:
            tab = self.new_tab(text, Path(path))
        note = f" (кодировка определена как {enc} — проверьте кириллицу)" if enc == "cp1251" else ""
        self.ctx.message.emit(f"Открыт файл {Path(path).name}{note}. Скрипт НЕ выполнялся.")

    def _save_tab(self, tab: EditorTab, save_as: bool) -> bool:
        path = tab.path
        if save_as or path is None:
            chosen, _ = QFileDialog.getSaveFileName(self, "Сохранить скрипт",
                                                    str(path or f"query{tab.uid}.sql"), "SQL (*.sql)")
            if not chosen:
                return False
            path = Path(chosen)
            if not path.suffix:
                path = path.with_suffix(".sql")
        try:
            write_sql_file(path, tab.editor.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Не удалось сохранить", f"{exc.strerror or exc}")
            return False
        tab.path = path
        tab.editor.document().setModified(False)
        self._retitle(tab)
        self.ctx.message.emit(f"Сохранено: {path}")
        return True

    def save_file(self, save_as: bool) -> None:
        tab = self.tab()
        if tab is not None:
            self._save_tab(tab, save_as)

    def _insert_sql(self, sql: str, new_tab: bool) -> None:
        tab = self.tab()
        if new_tab or tab is None:
            self.new_tab(sql)
        else:
            tab.editor.setPlainText(sql)
            tab.editor.document().setModified(True)

    def show_history(self) -> None:
        HistoryDialog(self, self.svc, self._insert_sql, self._add_fav_sql).exec()

    def show_favorites(self) -> None:
        FavoritesDialog(self, self.svc, self._insert_sql).exec()

    def _add_fav_sql(self, sql: str) -> None:
        title, ok = QInputDialog.getText(self, "В избранное", "Название запроса:", QLineEdit.EchoMode.Normal,
                                         " ".join(sql.split())[:60])
        if ok:
            self.svc.favorites.add(title, sql)
            self.ctx.message.emit("Запрос добавлен в избранное")

    def add_favorite(self) -> None:
        tab = self.tab()
        if tab is None:
            return
        sql, _ = tab.editor.sql_to_run()
        if not sql.strip():
            self.ctx.message.emit("Нечего добавлять: запрос пуст.")
            return
        self._add_fav_sql(sql)

    # ---- экспорт ---------------------------------------------------------
    def export_result(self) -> None:
        tab = self.tab()
        meta = tab.selected_meta() if tab else None
        if tab is None or meta is None:
            self.ctx.message.emit("Нет результата для экспорта: выберите вкладку с таблицей результата.")
            return
        analysis = analyze(tab.last_sql) if tab.last_sql else None
        reason = ""
        if analysis is None or not analysis.is_read_only:
            reason = ("Полная выгрузка повторно выполняет запрос и доступна только для запросов на чтение "
                      "(иначе изменения могли бы выполниться повторно).")
        elif tab.session is None:
            reason = "Сеанс закрыт — выполните запрос ещё раз."
        dlg = ExportDialog(self, settings=self.svc.settings, shown_rows=meta.model.rowCount(),
                           truncated=meta.truncated, full_allowed=not reason, full_reason=reason,
                           default_name=f"result{meta.number}")
        if dlg.exec() != ExportDialog.DialogCode.Accepted or not dlg.params:
            return
        p = dlg.params
        rows = None if p["full"] else list(meta.model.rows)
        columns = list(meta.model.columns)
        session, sql, number = tab.session, tab.last_sql, meta.number

        def job(ctx_):
            opts = {"delimiter": p["delimiter"], "formula_guard": p["formula_guard"]}
            try:
                if rows is not None:
                    return export_rows(p["path"], p["fmt"], columns, rows, cancel_token=ctx_.cancel_token,
                                       progress=lambda f: ctx_.report("Запись файла…", f), **opts)
                return session.export_full(sql, p["path"], p["fmt"], rowset_number=number,
                                           cancel_token=ctx_.cancel_token,
                                           progress=lambda n: ctx_.report(f"Выгружено строк: {n}"), **opts)
            except InterruptedError as exc:
                raise TaskCancelled() from exc
            except DbWorkspaceError as exc:
                if "отменена" in str(exc):
                    raise TaskCancelled() from exc
                raise

        def done(res):
            msg = f"Экспорт завершён: {res.rows} строк → {res.path}"
            tab.set_status(msg)
            self.ctx.message.emit(msg)
            if res.warnings:
                QMessageBox.information(self, "Экспорт завершён", msg + "\n\n" + "\n".join("• " + w for w in res.warnings))

        def failed(message):
            QMessageBox.critical(self, "Ошибка экспорта", message)

        try:
            self.ctx.runner.submit("Экспорт результата", job, on_success=done, on_error=failed,
                                   on_cancelled=lambda: self.ctx.message.emit("Экспорт отменён"),
                                   key=f"export:{tab.uid}")
        except DuplicateTaskError:
            self.ctx.message.emit("Экспорт этой вкладки уже выполняется.")
