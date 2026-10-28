"""Хранилище шаблонов отчётов (JSON): пользовательские и встроенные примеры."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from utils.paths import resource_path

from utils.files import atomic_write_text, sanitize_filename
from .model import SCHEMA_VERSION, ReportTemplate, new_id

log = logging.getLogger("dbworkspace.templates")
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
BUILTIN_DIR = resource_path("examples", "report_templates")


class TemplateError(Exception):
    pass


@dataclass(frozen=True)
class TemplateInfo:
    id: str
    name: str
    description: str
    builtin: bool
    path: Path


def parse_template(text: str) -> ReportTemplate:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TemplateError(f"Файл шаблона повреждён (JSON): {exc.msg}, строка {exc.lineno}.") from exc
    if not isinstance(data, dict):
        raise TemplateError("Файл не похож на шаблон отчёта.")
    ver = data.get("schema_version", 1)
    if not isinstance(ver, int) or ver > SCHEMA_VERSION:
        raise TemplateError("Шаблон создан более новой версией программы и не может быть открыт.")
    try:
        return ReportTemplate.from_dict(data)
    except (TypeError, ValueError, AttributeError) as exc:
        raise TemplateError(f"Шаблон содержит некорректные данные ({exc.__class__.__name__}).") from exc


def read_template_file(path: Path) -> ReportTemplate:
    try:
        if path.stat().st_size > MAX_TEMPLATE_BYTES:
            raise TemplateError("Файл шаблона слишком большой.")
        return parse_template(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise TemplateError(f"Не удалось прочитать файл: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise TemplateError("Файл шаблона не в кодировке UTF-8.") from exc


class TemplateStore:
    def __init__(self, user_dir: Path, builtin_dir: Path | None = BUILTIN_DIR):
        self.user_dir = Path(user_dir)
        self.builtin_dir = Path(builtin_dir) if builtin_dir else None
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.errors: list[str] = []

    def _scan(self, folder: Path | None, builtin: bool) -> list[tuple[TemplateInfo, ReportTemplate]]:
        out = []
        if folder is None or not folder.is_dir():
            return out
        for p in sorted(folder.glob("*.json")):
            try:
                t = read_template_file(p)
            except TemplateError as exc:
                self.errors.append(f"{p.name}: {exc}")
                continue
            out.append((TemplateInfo(t.id, t.name, t.description, builtin, p), t))
        return out

    def list(self) -> list[TemplateInfo]:
        self.errors = []
        items = self._scan(self.builtin_dir, True) + self._scan(self.user_dir, False)
        return [i for i, _ in items]

    def load(self, template_id: str) -> ReportTemplate:
        for info, t in self._scan(self.builtin_dir, True) + self._scan(self.user_dir, False):
            if info.id == template_id:
                return t
        raise TemplateError("Шаблон не найден.")

    def is_builtin(self, template_id: str) -> bool:
        return any(i.id == template_id for i, _ in self._scan(self.builtin_dir, True))

    def save(self, t: ReportTemplate) -> Path:
        """Сохраняет пользовательский шаблон. Встроенные шаблоны не перезаписываются."""
        if not t.id:
            t.id = new_id("t")
        if self.is_builtin(t.id):
            raise TemplateError("Встроенный шаблон нельзя изменить — сохраните его копию.")
        path = self.user_dir / f"{sanitize_filename(t.id, 'template')}.json"
        atomic_write_text(path, json.dumps(t.to_dict(), ensure_ascii=False, indent=2))
        return path

    def delete(self, template_id: str) -> None:
        if self.is_builtin(template_id):
            raise TemplateError("Встроенный шаблон удалить нельзя.")
        for info in self.list():
            if info.id == template_id and not info.builtin:
                info.path.unlink(missing_ok=True)
                return
        raise TemplateError("Шаблон не найден.")

    def duplicate(self, template_id: str, new_name: str = "") -> ReportTemplate:
        t = self.load(template_id)
        copy = ReportTemplate.from_dict(t.to_dict())
        copy.id = new_id("t")
        copy.name = new_name or f"{t.name} (копия)"
        self.save(copy)
        return copy

    def import_file(self, path: Path) -> ReportTemplate:
        t = read_template_file(Path(path))
        existing = {i.id for i in self.list()}
        if t.id in existing:
            t.id = new_id("t")                         # импорт не затирает существующие шаблоны
        self.save(t)
        return t

    def export_file(self, template_id: str, path: Path) -> Path:
        t = self.load(template_id)
        atomic_write_text(Path(path), json.dumps(t.to_dict(), ensure_ascii=False, indent=2))
        return Path(path)
