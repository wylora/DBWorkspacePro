"""Главное окно: боковая навигация, страницы, строка состояния."""
from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from bootstrap import AppServices

from . import theme
from .automation_page import AutomationPage
from .compare_page import ComparePage
from .connections_page import ConnectionsPage
from .converter_page import ConverterPage
from .documentation_page import DocumentationPage
from .context import AppContext
from .editor_page import EditorPage
from .explorer_page import ExplorerPage
from .help_page import HelpPage
from .history_page import HistoryPage
from .home_page import HomePage
from .monitoring_page import MonitoringPage
from .placeholder_page import PlaceholderPage
from .reports_page import ReportsPage
from .settings_page import SettingsPage
from .templates_page import TemplatesPage
from .tray import TrayController, app_icon

log = logging.getLogger("dbworkspace.ui")
from version import APP_VERSION_TEXT as APP_VERSION

# (ключ, название, этап реализации или None, описание для заглушки)
SECTIONS = [
    ("home", "Главная", None, ""),
    ("connections", "Подключения", None, ""),
    ("explorer", "Обозреватель", None, ""),
    ("editor", "SQL-редактор", None, ""),
    ("reports", "Отчёты Word", None, ""),
    ("templates", "Конструктор шаблонов", None, ""),
    ("converter", "Конвертер", None, ""),
    ("automation", "Автоматизация", None, ""),
    ("monitoring", "Мониторинг", None, ""),
    ("compare", "Сравнение БД", None, ""),
    ("docs", "Документация", None, ""),
    ("history", "История задач", None, ""),
    ("settings", "Настройки", None, ""),
    ("help", "Справка", None, ""),
]


class MainWindow(QMainWindow):
    def __init__(self, services: AppServices):
        super().__init__()
        self.services = services
        self.ctx = AppContext(services, self)
        self.setWindowTitle("DB Workspace Pro")
        self.setMinimumSize(980, 640)
        self.resize(1280, 800)
        self._pages: dict[str, QWidget] = {}
        self._build()
        self._restore_geometry()
        self.ctx.navigate.connect(self.show_page)
        self.ctx.message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.ctx.theme_changed.connect(self._apply_theme)
        self.ctx.connections_changed.connect(self._update_status)
        self.ctx.runner.task_changed.connect(lambda _t: self._update_status())
        self.ctx.notifications_changed.connect(self._update_notes)
        self.ctx.open_sql.connect(self._open_sql)
        self._apply_theme(self.ctx.theme)
        self._update_status()
        self._update_notes()
        self.show_page("home")
        self.setWindowIcon(app_icon())
        self._install_shortcuts()
        self.tray = TrayController(self, self.ctx)
        try:
            self.ctx.automation.start()          # встроенный планировщик (если включён в настройках)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось запустить планировщик")

    # ---- построение --------------------------------------------------
    def _build(self) -> None:
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QWidget()
        side.setFixedWidth(240)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        brand = QLabel("DB Workspace Pro")
        brand.setObjectName("SidebarBrand")
        self.nav = QListWidget()
        self.nav.setObjectName("Sidebar")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ver = QLabel(f"Версия {APP_VERSION}")
        ver.setObjectName("SidebarVersion")
        sl.addWidget(brand)
        sl.addWidget(self.nav, 1)
        sl.addWidget(ver)
        lay.addWidget(side)

        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        real = {
            "home": lambda: HomePage(self.ctx),
            "connections": lambda: ConnectionsPage(self.ctx),
            "explorer": lambda: ExplorerPage(self.ctx),
            "editor": lambda: EditorPage(self.ctx),
            "reports": lambda: ReportsPage(self.ctx),
            "templates": lambda: TemplatesPage(self.ctx),
            "converter": lambda: ConverterPage(self.ctx),
            "automation": lambda: AutomationPage(self.ctx),
            "monitoring": lambda: MonitoringPage(self.ctx),
            "compare": lambda: ComparePage(self.ctx),
            "docs": lambda: DocumentationPage(self.ctx),
            "help": lambda: HelpPage(self.ctx),
            "history": lambda: HistoryPage(self.ctx),
            "settings": lambda: SettingsPage(self.ctx),
        }
        for key, title, stage, planned in SECTIONS:
            if stage is None:
                page = real[key]()
                text = title
            else:
                page = PlaceholderPage(title, stage, planned)
                text = f"{title}   ·  этап {stage}"
            self._pages[key] = page
            self.stack.addWidget(page)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            if stage is not None:
                item.setToolTip(f"Раздел будет реализован на этапе {stage}")
            self.nav.addItem(item)
        self.nav.currentItemChanged.connect(self._on_nav)

        sb = self.statusBar()
        self.lbl_conn = QLabel("")
        self.lbl_tasks = QLabel("")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(120)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.btn_cancel_all = QPushButton("Отменить задачи")
        self.btn_cancel_all.setToolTip("Отменить все выполняющиеся и ожидающие фоновые задачи")
        self.btn_cancel_all.clicked.connect(self._cancel_all_tasks)
        self.btn_cancel_all.hide()
        self.btn_notes = QPushButton("")
        self.btn_notes.setToolTip("Непрочитанные уведомления")
        self.btn_notes.clicked.connect(self._open_notes)
        self.btn_notes.hide()
        sb.addPermanentWidget(self.btn_notes)
        sb.addPermanentWidget(self.btn_cancel_all)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.lbl_tasks)
        sb.addPermanentWidget(self.lbl_conn)

    # ---- навигация ---------------------------------------------------
    def _on_nav(self, current, _prev) -> None:
        if current is not None:
            self.stack.setCurrentWidget(self._pages[current.data(Qt.ItemDataRole.UserRole)])

    def show_page(self, key: str) -> None:
        for i in range(self.nav.count()):
            if self.nav.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.nav.setCurrentRow(i)
                return

    # ---- состояние ---------------------------------------------------
    def _update_status(self) -> None:
        m = self.services.manager
        active = self.services.tasks.tasks(active_only=True)
        self.lbl_conn.setText(f"Подключений: {m.count()} / {m.max_connections}")
        if active:
            self.lbl_tasks.setText(f"Задачи: {len(active)} — {active[0].stage or active[0].name}")
            self.progress.show()
            self.btn_cancel_all.show()
        else:
            self.btn_cancel_all.hide()
            self.lbl_tasks.setText("Задач нет")
            self.progress.hide()

    def _install_shortcuts(self) -> None:
        keys = [k for k, *_ in SECTIONS]
        for i, key in enumerate(keys[:9]):
            sc = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            sc.activated.connect(lambda k=key: self.show_page(k))
        QShortcut(QKeySequence("F1"), self).activated.connect(lambda: self.show_page("help"))
        QShortcut(QKeySequence("Ctrl+Shift+N"), self).activated.connect(self._open_notes)

    def _open_sql(self, sql: str) -> None:
        self.show_page("editor")
        self._pages["editor"].new_tab(sql)

    def _update_notes(self) -> None:
        n = self.ctx.automation.notes.unread_count()
        self.btn_notes.setText(f"Уведомления: {n}")
        self.btn_notes.setVisible(n > 0)

    def _open_notes(self) -> None:
        self.show_page("automation")
        self._pages["automation"].show_notifications()

    def _cancel_all_tasks(self) -> None:
        for t in self.services.tasks.tasks(active_only=True):
            self.services.tasks.cancel(t.id)
        self.statusBar().showMessage("Запрошена отмена задач", 5000)

    def _apply_theme(self, name: str) -> None:
        theme.apply_theme(QApplication.instance(), name)

    # ---- геометрия и закрытие ----------------------------------------
    def _restore_geometry(self) -> None:
        data = self.services.settings.get("window_geometry")
        if data:
            try:
                self.restoreGeometry(QByteArray.fromBase64(data.encode()))
            except Exception:  # noqa: BLE001
                log.warning("Не удалось восстановить геометрию окна")

    def closeEvent(self, event):  # noqa: N802
        if not self._pages["editor"].prepare_close():     # несохранённые скрипты, транзакции, выполняющиеся запросы
            event.ignore()
            return
        if not self._pages["templates"].prepare_close():  # несохранённый шаблон отчёта
            event.ignore()
            return
        active = self.services.tasks.tasks(active_only=True)
        if active:
            answer = QMessageBox.question(
                self, "Выход", f"Выполняются фоновые задачи ({len(active)}). Отменить их и выйти?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        try:
            self.services.settings.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode())
            self._pages["explorer"].save_state()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить состояние окна")
        self.ctx.automation.stop()
        self.tray.hide()
        self.services.shutdown()
        event.accept()
