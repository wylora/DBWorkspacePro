"""Справка: руководство пользователя, о программе, диагностика."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from converter.docmodel import parse_markdown, to_html
from utils.diagnostics import collect_diagnostics
from utils.env_check import format_report, run_checks
from utils.paths import resource_path
from version import APP_VERSION_TEXT

from .context import AppContext
from .helpers import open_path
from .widgets import PageHeader

GUIDES = [("Руководство пользователя", "user_guide.md"), ("Руководство администратора", "admin_guide.md"),
          ("Что нового", "CHANGELOG.md")]


def load_guide(name: str) -> str:
    path = resource_path("docs", name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return f"<p>Файл справки не найден: {path}</p>"
    return to_html(parse_markdown(text).blocks, name)


class HelpPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader("Справка", f"DB Workspace Pro {APP_VERSION_TEXT}"))
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.browsers: list[QTextBrowser] = []
        for title, name in GUIDES:
            w = QWidget()
            lay = QVBoxLayout(w)
            search = QLineEdit()
            search.setPlaceholderText("Поиск по тексту (Enter — следующее совпадение)")
            br = QTextBrowser()
            br.setOpenExternalLinks(False)
            br.setHtml(load_guide(name))
            search.returnPressed.connect(lambda b=br, s=search: self._find(b, s.text()))
            lay.addWidget(search)
            lay.addWidget(br, 1)
            self.browsers.append(br)
            self.tabs.addTab(w, title)
        about = QWidget()
        al = QVBoxLayout(about)
        p = self.svc.paths
        info = QLabel(f"<b>DB Workspace Pro {APP_VERSION_TEXT}</b><br>Данные программы: {p.data}<br>Журналы: {p.logs}<br>"
                      "Пароли хранятся только в системном хранилище учётных данных Windows.")
        info.setWordWrap(True)
        al.addWidget(info)
        row = QHBoxLayout()
        for text, fn in (("Открыть папку данных", lambda: open_path(str(p.data))),
                         ("Открыть журналы", lambda: open_path(str(p.logs))),
                         ("Проверить окружение", self.check_env), ("Собрать диагностический архив…", self.diagnostics)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        al.addLayout(row)
        self.env = QPlainTextEdit()
        self.env.setReadOnly(True)
        al.addWidget(self.env, 1)
        self.tabs.addTab(about, "О программе")

    @staticmethod
    def _find(browser: QTextBrowser, text: str) -> None:
        if text and not browser.find(text):
            browser.moveCursor(browser.textCursor().MoveOperation.Start)
            browser.find(text)

    def check_env(self) -> None:
        self.env.setPlainText(format_report(run_checks(self.svc.paths.home)))

    def diagnostics(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Диагностический архив",
                                              f"DBWorkspacePro_диагностика_{dt.datetime.now():%Y%m%d_%H%M}.zip", "ZIP (*.zip)")
        if not path:
            return
        try:
            collect_diagnostics(self.svc, Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "Диагностика", str(exc))
            return
        QMessageBox.information(self, "Диагностика", "Архив сохранён. Пароли в нём замаскированы; список подключений и "
                                                     "история запросов не включены. Журналы могут содержать имена серверов "
                                                     "и баз данных — просмотрите архив перед отправкой.")

    def show_tab(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)
