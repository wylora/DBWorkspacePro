"""Расположение данных приложения (настройки, журналы, временные файлы)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "DBWorkspacePro"


def resource_root() -> Path:
    """Корень ресурсов: папка проекта или распакованная сборка PyInstaller (sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parents[1]


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def app_home(override: str | os.PathLike | None = None) -> Path:
    """Корневая папка данных приложения.

    Приоритет: явный аргумент -> переменная окружения DBWP_HOME ->
    %APPDATA%\\DBWorkspacePro (Windows) -> ~/.local/share/DBWorkspacePro.
    """
    if override:
        return Path(override)
    env = os.environ.get("DBWP_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / APP_NAME


class AppPaths:
    """Набор путей приложения. Папки создаются методом ensure()."""

    def __init__(self, home: str | os.PathLike | None = None):
        self.home = app_home(home)

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def temp(self) -> Path:
        """Отдельная рабочая папка для временных файлов."""
        return self.home / "tmp"

    @property
    def db_file(self) -> Path:
        return self.data / "app.sqlite3"

    @property
    def locks(self) -> Path:
        return self.data / "locks"

    def ensure(self) -> "AppPaths":
        for p in (self.home, self.logs, self.data, self.temp):
            p.mkdir(parents=True, exist_ok=True)
        return self
