"""Обозреватель объектов: серверы → базы → таблицы/представления/процедуры/функции/триггеры.

Все запросы метаданных выполняются в фоновых задачах; узлы загружаются лениво при раскрытии.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QHBoxLayout, QLineEdit, QMenu, QPlainTextEdit,
    QPushButton, QSplitter, QStackedLayout, QTableWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from database.connection import STATE_LABELS, SqlConnection
from database.metadata import MetadataReader, ObjectInfo, TableDetails
from database.sqltext import qualified_name
from utils.tasks import DuplicateTaskError

from .context import AppContext
from .widgets import EmptyState, PageHeader

log = logging.getLogger("dbworkspace.explorer")
ROLE = Qt.ItemDataRole.UserRole

FOLDERS = [("table", "Таблицы"), ("view", "Представления"), ("procedure", "Хранимые процедуры"),
           ("function", "Функции"), ("trigger", "Триггеры")]
MODULE_KINDS = ("view", "procedure", "function", "trigger")


class ExplorerPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self._generation = 0
        self._build()
        ctx.connections_changed.connect(self._sync_servers)
        self._sync_servers()

    # ---- интерфейс ---------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Обозреватель объектов",
            "Базы данных, таблицы, представления, столбцы, ключи, индексы, процедуры, функции и триггеры. "
            "Показываются только объекты, метаданные которых доступны вашей учётной записи."))

        bar = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фильтр по уже загруженным узлам…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.btn_refresh = QPushButton("Обновить")
        self.btn_refresh.clicked.connect(self._refresh_all)
        bar.addWidget(self.filter, 1)
        bar.addWidget(self.btn_refresh)
        root.addLayout(bar)

        holder = QWidget()
        self.stack = QStackedLayout(holder)
        root.addWidget(holder, 1)

        self.empty = EmptyState(
            "Нет активных подключений",
            "Подключитесь к SQL Server на странице «Подключения» — после этого здесь появится дерево объектов.",
            "Перейти к подключениям")
        self.empty.action_clicked.connect(lambda: self.ctx.navigate.emit("connections"))
        self.stack.addWidget(self.empty)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.currentItemChanged.connect(self._on_current)
        self.splitter.addWidget(self.tree)

        right = QSplitter(Qt.Orientation.Vertical)
        self.props = QTableWidget(0, 2)
        self.props.setHorizontalHeaderLabels(["Свойство", "Значение"])
        self.props.horizontalHeader().setStretchLastSection(True)
        self.props.verticalHeader().setVisible(False)
        self.props.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.props.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.props.setColumnWidth(0, 200)
        right.addWidget(self.props)
        self.definition = QPlainTextEdit()
        self.definition.setReadOnly(True)
        self.definition.setPlaceholderText("Определение (T-SQL) для представлений, процедур, функций и триггеров")
        self.definition.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        right.addWidget(self.definition)
        right.setSizes([260, 260])
        self.splitter.addWidget(right)
        self.splitter.setSizes([420, 520])
        self.stack.addWidget(self.splitter)

        state = self.svc.settings.get("explorer_splitter")
        if state:
            self.splitter.restoreState(QByteArray.fromBase64(state.encode()))

    def save_state(self) -> None:
        self.svc.settings.set("explorer_splitter", bytes(self.splitter.saveState().toBase64()).decode())

    # ---- дерево: серверы ---------------------------------------------
    def _server_items(self) -> dict[str, QTreeWidgetItem]:
        out = {}
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            node = it.data(0, ROLE)
            out[node["conn_id"]] = it
        return out

    def _sync_servers(self) -> None:
        conns = {c.id: c for c in self.svc.manager.connections()
                 if self.svc.manager.primary(c.profile.id) is c}
        existing = self._server_items()
        for cid, item in existing.items():
            if cid not in conns:
                self._generation += 1
                self.tree.invisibleRootItem().removeChild(item)
        for cid, conn in conns.items():
            if cid not in existing:
                item = QTreeWidgetItem(self.tree, [""])
                item.setData(0, ROLE, {"type": "server", "conn_id": cid, "loaded": False})
                self._add_placeholder(item)
                existing[cid] = item
            existing[cid].setText(0, f"{conn.profile.name}  —  {STATE_LABELS[conn.state]}")
        self.stack.setCurrentIndex(1 if conns else 0)
        self.btn_refresh.setEnabled(bool(conns))

    def _conn(self, item: QTreeWidgetItem) -> SqlConnection | None:
        top = item
        while top.parent() is not None:
            top = top.parent()
        cid = top.data(0, ROLE)["conn_id"]
        for c in self.svc.manager.connections():
            if c.id == cid:
                return c
        return None

    # ---- ленивая загрузка --------------------------------------------
    @staticmethod
    def _add_placeholder(item: QTreeWidgetItem) -> None:
        ph = QTreeWidgetItem(item, ["Загрузка…"])
        ph.setData(0, ROLE, {"type": "placeholder"})

    @staticmethod
    def _clear_children(item: QTreeWidgetItem) -> None:
        for child in item.takeChildren():
            del child

    def _child(self, parent: QTreeWidgetItem, text: str, node: dict, expandable: bool = False) -> QTreeWidgetItem:
        it = QTreeWidgetItem(parent, [text])
        it.setData(0, ROLE, node)
        if expandable:
            self._add_placeholder(it)
        return it

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, ROLE)
        if not node or node.get("loaded"):
            return
        t = node["type"]
        if t == "server":
            self._load_databases(item)
        elif t == "database":
            self._clear_children(item)
            for kind, title in FOLDERS:
                self._child(item, title, {"type": "folder", "kind": kind, "db": node["db"], "loaded": False}, True)
            node["loaded"] = True
        elif t == "folder":
            self._load_folder(item)
        elif t == "object" and node["kind"] in ("table", "view"):
            self._load_details(item)

    def _run(self, item: QTreeWidgetItem, title: str, work, on_success) -> None:
        conn = self._conn(item)
        if conn is None:
            self._fail(item, "Подключение закрыто.")
            return
        gen = self._generation
        node = item.data(0, ROLE)
        key = f"explore:{conn.id}:{title}:{node.get('db', '')}:{node.get('object_id', '')}"
        batch = self.svc.settings.get("fetch_batch_size")

        def job(ctx_):
            return work(MetadataReader(conn, batch_size=batch), ctx_)

        def ok(result):
            if gen != self._generation:
                return
            on_success(result)

        def err(msg):
            if gen != self._generation:
                return
            self._fail(item, msg)

        try:
            self.ctx.runner.submit(f"Обозреватель: {title}", job, on_success=ok, on_error=err, key=key)
        except DuplicateTaskError:
            pass

    def _fail(self, item: QTreeWidgetItem, message: str) -> None:
        self._clear_children(item)
        err = QTreeWidgetItem(item, [f"⚠ {message}"])
        err.setData(0, ROLE, {"type": "error", "message": message})
        node = item.data(0, ROLE)
        node["loaded"] = False  # позволит повторить при следующем раскрытии/обновлении
        self.ctx.message.emit(message)

    def _load_databases(self, item: QTreeWidgetItem) -> None:
        def done(names):
            self._clear_children(item)
            for n in names:
                self._child(item, n, {"type": "database", "db": n, "loaded": False}, True)
            if not names:
                self._child(item, "(нет доступных баз данных)", {"type": "info"})
            item.data(0, ROLE)["loaded"] = True
        self._run(item, "базы данных", lambda r, c: r.list_databases(), done)

    def _load_folder(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, ROLE)
        kind, db = node["kind"], node["db"]

        def done(res):
            self._clear_children(item)
            for obj in res.items:
                label = obj.full_name if kind != "trigger" or not obj.parent else f"{obj.full_name}  (на {obj.parent})"
                self._child(item, label, {"type": "object", "kind": kind, "db": db, "info": obj,
                                          "object_id": obj.object_id, "loaded": False},
                            expandable=kind in ("table", "view"))
            if not res.items:
                self._child(item, "(пусто)", {"type": "info"})
            if res.truncated:
                self._child(item, f"… показаны первые {len(res.items)} объектов", {"type": "info"})
            title = dict(FOLDERS)[kind]
            item.setText(0, f"{title} ({len(res.items)}{'+' if res.truncated else ''})")
            node["loaded"] = True
        self._run(item, f"{db}: {dict(FOLDERS)[kind].lower()}", lambda r, c: r.list_objects(db, kind), done)

    def _load_details(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, ROLE)
        db, oid = node["db"], node["object_id"]

        def done(d: TableDetails):
            self._clear_children(item)
            node["details"] = d
            cols = self._child(item, f"Столбцы ({len(d.columns)})", {"type": "info"})
            for c in d.columns:
                prefix = "PK  " if c.is_primary_key else ""
                self._child(cols, f"{prefix}{c.name}  ({c.describe()})", {"type": "column", "column": c})
            if d.indexes:
                folder = self._child(item, f"Индексы и ключи ({len(d.indexes)})", {"type": "info"})
                for ix in d.indexes:
                    self._child(folder, self._index_text(ix), {"type": "index", "index": ix})
            if d.foreign_keys:
                folder = self._child(item, f"Внешние ключи ({len(d.foreign_keys)})", {"type": "info"})
                for fk in d.foreign_keys:
                    self._child(folder, self._fk_text(fk), {"type": "fk", "fk": fk})
            for w in d.warnings:
                self._child(item, f"⚠ {w}", {"type": "info"})
            node["loaded"] = True
            self.tree.expandItem(cols)
            if self.tree.currentItem() is item:
                self._show_properties(item)
        self._run(item, f"{db}: {node['info'].full_name}", lambda r, c: r.get_table_details(db, oid), done)

    @staticmethod
    def _index_text(ix) -> str:
        flags = [ix.type_desc]
        if ix.is_primary_key:
            flags.append("PRIMARY KEY")
        elif ix.is_unique_constraint:
            flags.append("UNIQUE CONSTRAINT")
        elif ix.is_unique:
            flags.append("UNIQUE")
        inc = f" INCLUDE ({', '.join(ix.included_columns)})" if ix.included_columns else ""
        return f"{ix.name} — {', '.join(flags)}: ({', '.join(ix.columns)}){inc}"

    @staticmethod
    def _fk_text(fk) -> str:
        off = " [отключён]" if fk.is_disabled else ""
        return (f"{fk.name}: ({', '.join(fk.columns)}) → {fk.ref_schema}.{fk.ref_table} "
                f"({', '.join(fk.ref_columns)}){off}")

    # ---- обновление и фильтр -----------------------------------------
    def _refresh_all(self) -> None:
        self._generation += 1
        self.tree.clear()
        self.props.setRowCount(0)
        self.definition.clear()
        self._sync_servers()

    def _refresh_node(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, ROLE)
        self._generation += 1
        self._clear_children(item)
        node["loaded"] = False
        node.pop("details", None)
        node.pop("definition", None)
        if node["type"] == "folder":
            item.setText(0, dict(FOLDERS)[node["kind"]])
        self._add_placeholder(item)
        if item.isExpanded():
            self._on_expanded(item)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()

        def visit(item: QTreeWidgetItem, ancestor_match: bool) -> bool:
            own = bool(needle) and needle in item.text(0).lower()
            matched = (not needle) or own or ancestor_match
            child_visible = False
            for i in range(item.childCount()):
                if visit(item.child(i), own or ancestor_match):
                    child_visible = True
            visible = matched or child_visible
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i), False)

    # ---- свойства и определение --------------------------------------
    def _on_current(self, current, _previous) -> None:
        if current is None:
            self.props.setRowCount(0)
            self.definition.clear()
            return
        self._show_properties(current)

    def _set_props(self, rows: list[tuple[str, str]]) -> None:
        self.props.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.props.setItem(r, 0, QTableWidgetItem(k))
            self.props.setItem(r, 1, QTableWidgetItem(str(v)))

    def _show_properties(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, ROLE) or {}
        t = node.get("type")
        self.definition.clear()
        rows: list[tuple[str, str]] = []
        if t == "server":
            conn = self._conn(item)
            if conn:
                p, si = conn.profile, conn.server_info
                rows = [("Подключение", p.name), ("Сервер", p.server_spec()), ("Состояние", STATE_LABELS[conn.state]),
                        ("Версия SQL Server", si.version), ("Редакция", si.edition), ("Имя сервера", si.server_name),
                        ("ODBC-драйвер", conn.driver_used), ("Аутентификация", "Windows" if p.auth == "windows" else "SQL Server"),
                        ("Шифрование", p.encrypt),
                        ("Проверка сертификата", "ОТКЛЮЧЕНА (доверие сертификату)" if p.trust_server_certificate else "включена")]
        elif t == "database":
            rows = [("База данных", node["db"])]
        elif t == "object":
            o: ObjectInfo = node["info"]
            rows = [("Имя", o.name), ("Схема", o.schema), ("Полное имя", qualified_name(node["db"], o.schema, o.name)),
                    ("Тип", o.type_desc or o.kind), ("Создан", o.created.strftime("%Y-%m-%d %H:%M") if o.created else "—"),
                    ("object_id", o.object_id)]
            if o.parent:
                rows.append(("Родительский объект", o.parent))
            d = node.get("details")
            if d is not None and d.row_estimate is not None:
                rows.append(("Строк (приблизительно)", f"{d.row_estimate:,}".replace(",", " ")))
            if o.kind in MODULE_KINDS:
                self._load_definition(item)
        elif t == "column":
            c = node["column"]
            rows = [("Имя", c.name), ("Тип данных", c.data_type), ("Допускает NULL", "да" if c.is_nullable else "нет"),
                    ("Первичный ключ", "да" if c.is_primary_key else "нет"), ("IDENTITY", "да" if c.is_identity else "нет"),
                    ("Вычисляемый", "да" if c.is_computed else "нет"), ("Значение по умолчанию", c.default or "—"),
                    ("Порядковый номер", c.ordinal)]
        elif t == "index":
            ix = node["index"]
            rows = [("Имя", ix.name), ("Тип", ix.type_desc), ("Уникальный", "да" if ix.is_unique else "нет"),
                    ("Первичный ключ", "да" if ix.is_primary_key else "нет"), ("Столбцы", ", ".join(ix.columns)),
                    ("Включённые столбцы", ", ".join(ix.included_columns) or "—")]
        elif t == "fk":
            fk = node["fk"]
            rows = [("Имя", fk.name), ("Столбцы", ", ".join(fk.columns)),
                    ("Ссылается на", f"{fk.ref_schema}.{fk.ref_table} ({', '.join(fk.ref_columns)})"),
                    ("ON DELETE", fk.on_delete), ("ON UPDATE", fk.on_update),
                    ("Отключён", "да" if fk.is_disabled else "нет")]
        elif t == "error":
            rows = [("Ошибка", node["message"])]
        self._set_props(rows)

    def _load_definition(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, ROLE)
        if "definition" in node:
            self.definition.setPlainText(node["definition"])
            return
        db, oid = node["db"], node["object_id"]
        self.definition.setPlainText("Загрузка определения…")

        def done(text):
            node["definition"] = text or ("-- Определение недоступно: нет права VIEW DEFINITION "
                                          "или объект зашифрован.")
            if self.tree.currentItem() is item:
                self.definition.setPlainText(node["definition"])
        self._run(item, f"{db}: определение {node['info'].full_name}",
                  lambda r, c: r.get_definition(db, oid), done)

    # ---- контекстное меню --------------------------------------------
    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        node = item.data(0, ROLE) or {}
        t = node.get("type")
        menu = QMenu(self)
        clip = QApplication.clipboard()
        if t == "server":
            conn = self._conn(item)
            if conn:
                menu.addAction("Копировать адрес сервера", lambda: clip.setText(conn.profile.server_spec()))
                menu.addAction("Переподключиться", lambda: self._reconnect(item))
                menu.addAction("Отключиться", lambda: self._disconnect(item))
        elif t == "database":
            menu.addAction("Копировать имя", lambda: clip.setText(node["db"]))
        elif t == "object":
            o: ObjectInfo = node["info"]
            menu.addAction("Копировать имя", lambda: clip.setText(o.name))
            menu.addAction("Копировать полное имя", lambda: clip.setText(qualified_name(node["db"], o.schema, o.name)))
            if "definition" in node:
                menu.addAction("Копировать определение", lambda: clip.setText(node["definition"]))
        elif t in ("column", "index", "fk"):
            name = (node.get("column") or node.get("index") or node.get("fk")).name
            menu.addAction("Копировать имя", lambda: clip.setText(name))
        if t in ("server", "database", "folder", "object"):
            menu.addSeparator()
            menu.addAction("Обновить", lambda: self._refresh_node(item))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _reconnect(self, item: QTreeWidgetItem) -> None:
        conn = self._conn(item)
        if conn is None:
            return
        self.ctx.runner.submit(f"Переподключение к «{conn.profile.name}»", lambda c: conn.reconnect(),
                               on_success=lambda _r: (self.ctx.connections_changed.emit(), self._refresh_node(item)),
                               on_error=lambda m: (self.ctx.message.emit(m), self.ctx.connections_changed.emit()),
                               key=f"conn:{conn.profile.id}")

    def _disconnect(self, item: QTreeWidgetItem) -> None:
        conn = self._conn(item)
        if conn is None:
            return
        pid, name = conn.profile.id, conn.profile.name
        self.ctx.runner.submit(f"Отключение от «{name}»", lambda c: self.svc.manager.disconnect_profile(pid),
                               on_success=lambda _r: self.ctx.connections_changed.emit(), key=f"conn:{pid}")
