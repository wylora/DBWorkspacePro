"""Мелкие общие функции интерфейса."""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def open_path(path: str) -> bool:
    """Открывает файл или папку программой по умолчанию (только локальные пути)."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))
