"""Настройки приложения с проверкой допустимых значений."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .db import AppDatabase


@dataclass(frozen=True)
class SettingSpec:
    default: Any
    kind: type
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple | None = None


SPECS: dict[str, SettingSpec] = {
    "theme": SettingSpec("light", str, choices=("light", "dark")),
    "max_connections": SettingSpec(5, int, 1, 20),
    "connect_timeout": SettingSpec(15, int, 1, 300),
    "query_timeout": SettingSpec(60, int, 0, 86400),  # 0 = без ограничения
    "fetch_batch_size": SettingSpec(1000, int, 10, 100_000),
    "preview_row_limit": SettingSpec(1000, int, 1, 1_000_000),
    "max_parallel_tasks": SettingSpec(3, int, 1, 16),
    "log_level": SettingSpec("INFO", str, choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    "window_geometry": SettingSpec("", str),
    "explorer_splitter": SettingSpec("", str),
    "last_profile_id": SettingSpec("", str),
    "editor_font_size": SettingSpec(11, int, 8, 32),
    "query_history_enabled": SettingSpec(True, bool),
    "query_history_limit": SettingSpec(500, int, 10, 100_000),
    "export_formula_guard": SettingSpec(True, bool),
    "report_output_dir": SettingSpec("", str),
    "report_max_rows": SettingSpec(5000, int, 1, 100_000),
    "report_pdf_timeout": SettingSpec(180, int, 30, 1800),
    "libreoffice_path": SettingSpec("", str),
    "report_overwrite": SettingSpec("new_version", str, choices=("new_version", "overwrite", "skip")),
    "report_formats": SettingSpec("docx", str),
    "convert_output_dir": SettingSpec("", str),
    "convert_policy": SettingSpec("new_version", str, choices=("new_version", "overwrite", "skip")),
    "convert_workers": SettingSpec(1, int, 1, 3),
    "convert_timeout": SettingSpec(300, int, 30, 3600),
    "convert_same_dir": SettingSpec(False, bool),
    "convert_last_format": SettingSpec("pdf", str),
    "scheduler_enabled": SettingSpec(True, bool),
    "monitor_interval": SettingSpec(60, int, 10, 3600),
    "doc_output_dir": SettingSpec("", str),
    "compare_max_rows": SettingSpec(200_000, int, 1_000, 2_000_000),
    "tray_notifications": SettingSpec(True, bool),
    "export_csv_delimiter": SettingSpec(";", str, choices=(";", ",", "\t")),
}


def validate(key: str, value: Any) -> Any:
    spec = SPECS.get(key)
    if spec is None:
        raise KeyError(f"Неизвестная настройка: {key}")
    if spec.kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Настройка {key}: ожидается целое число")
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"Настройка {key}: минимум {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"Настройка {key}: максимум {spec.maximum}")
    elif spec.kind is bool:
        if not isinstance(value, bool):
            raise ValueError(f"Настройка {key}: ожидается да/нет")
    elif spec.kind is str:
        if not isinstance(value, str):
            raise ValueError(f"Настройка {key}: ожидается строка")
    if spec.choices is not None and value not in spec.choices:
        raise ValueError(f"Настройка {key}: допустимо {', '.join(spec.choices)}")
    return value


class SettingsStore:
    def __init__(self, db: AppDatabase):
        self._db = db

    def get(self, key: str) -> Any:
        spec = SPECS[key]
        rows = self._db.query("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return spec.default
        try:
            return validate(key, json.loads(rows[0]["value"]))
        except (ValueError, json.JSONDecodeError):
            return spec.default  # повреждённое значение -> значение по умолчанию

    def set(self, key: str, value: Any) -> None:
        validate(key, value)
        self._db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def all(self) -> dict[str, Any]:
        return {k: self.get(k) for k in SPECS}
