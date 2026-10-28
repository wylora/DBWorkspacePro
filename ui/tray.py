"""Значок в области уведомлений Windows: уведомления об ошибках и предупреждениях задач."""
from __future__ import annotations

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from utils.paths import resource_path


class TrayController:
    def __init__(self, window, ctx):
        self.window, self.ctx = window, ctx
        self._last_id = 0
        self.icon = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.icon = QSystemTrayIcon(app_icon(), window)
        self.icon.setToolTip("DB Workspace Pro")
        menu = QMenu()
        menu.addAction("Открыть окно", self._show)
        menu.addAction("Уведомления", self._notes)
        menu.addSeparator()
        menu.addAction("Выход", window.close)
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(lambda reason: self._show() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.icon.messageClicked.connect(self._notes)
        self.icon.show()
        latest = ctx.automation.notes.recent(1)
        self._last_id = latest[0].id if latest else 0
        ctx.notifications_changed.connect(self.on_notes)

    def _show(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _notes(self) -> None:
        self._show()
        self.window._open_notes()

    def on_notes(self) -> None:
        if self.icon is None or not self.ctx.services.settings.get("tray_notifications"):
            return
        new = [n for n in self.ctx.automation.notes.recent(10) if n.id > self._last_id]
        if not new:
            return
        self._last_id = max(n.id for n in new)
        important = [n for n in new if n.level in ("error", "warning")]
        if not important or self.window.isActiveWindow():
            return
        n = important[0]
        level = QSystemTrayIcon.MessageIcon.Critical if n.level == "error" else QSystemTrayIcon.MessageIcon.Warning
        self.icon.showMessage(n.title, n.message[:240] + (f"\n(и ещё {len(important) - 1})" if len(important) > 1 else ""),
                              level, 8000)

    def hide(self) -> None:
        if self.icon is not None:
            self.icon.hide()


def app_icon() -> QIcon:
    path = resource_path("packaging", "app.ico")
    return QIcon(str(path)) if path.exists() else QIcon()
