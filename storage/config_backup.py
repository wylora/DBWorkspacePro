"""Резервная копия настроек программы (экспорт / импорт).

Пароли не экспортируются никогда (они хранятся в системном хранилище учётных данных).
При импорте задачи по расписанию приходят выключенными, а подтверждения изменяющих SQL-сценариев снимаются:
файл резервной копии мог быть изменён, и ничего не должно запускаться без проверки пользователем.
"""
from __future__ import annotations

import datetime as dt
import json
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bootstrap import AppServices
from database.profile import ConnectionProfile
from version import __version__

FORMAT = "dbworkspacepro-config"
FORMAT_VERSION = 1
SECTIONS = {"settings": "Настройки", "profiles": "Подключения (без паролей)", "favorites": "Избранные запросы",
            "templates": "Шаблоны отчётов", "scenarios": "SQL-сценарии", "tasks": "Задачи по расписанию"}
MAX_ENTRY = 50 * 1048576
SECRET_SETTINGS: set[str] = set()          # настроек с секретами нет; оставлено для будущего


class ConfigBackupError(Exception):
    pass


@dataclass
class ImportReport:
    added: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def bump(self, kind: str, section: str) -> None:
        d = getattr(self, kind)
        d[section] = d.get(section, 0) + 1

    def summary(self) -> str:
        parts = []
        for sec, title in SECTIONS.items():
            a, u, s = self.added.get(sec, 0), self.updated.get(sec, 0), self.skipped.get(sec, 0)
            if a or u or s:
                parts.append(f"{title}: добавлено {a}, обновлено {u}" + (f", пропущено {s}" if s else ""))
        return "\n".join(parts) or "Нечего импортировать."


def _automation(svc: AppServices):
    from automation.service import AutomationService
    return AutomationService(svc, background=False)


def _templates(svc: AppServices):
    from reports.service import ReportService
    return ReportService(svc).templates


def export_config(svc: AppServices, path: Path, sections: set[str] | None = None) -> dict[str, int]:
    sections = set(sections or SECTIONS)
    data: dict[str, object] = {}
    if "settings" in sections:
        data["settings"] = {k: v for k, v in svc.settings.all().items() if k not in SECRET_SETTINGS}
    if "profiles" in sections:
        data["profiles"] = [asdict(p) for p in svc.profiles.repo.list()]
    if "favorites" in sections:
        data["favorites"] = [{"title": f.title, "sql": f.sql} for f in svc.favorites.list()]
    if "templates" in sections:
        store = _templates(svc)
        data["templates"] = [store.load(i.id).to_dict() for i in store.list() if not i.builtin]
    auto = _automation(svc) if sections & {"scenarios", "tasks"} else None
    if "scenarios" in sections:
        data["scenarios"] = [asdict(s) for s in auto.scenarios.list()]
    if "tasks" in sections:
        data["tasks"] = [{"id": t.id, "name": t.name, "kind": t.kind, "enabled": t.enabled, "catch_up": t.catch_up,
                          "schedule": t.schedule.to_dict(), "config": t.config} for t in auto.tasks.list()]
    counts = {k: (len(v) if isinstance(v, list) else len(v)) for k, v in data.items()}
    manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "app_version": __version__,
                "created": dt.datetime.now().isoformat(timespec="seconds"), "sections": counts,
                "note": "Пароли не включены. Задачи при импорте выключаются, подтверждения сценариев снимаются."}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for k, v in data.items():
            z.writestr(f"{k}.json", json.dumps(v, ensure_ascii=False, indent=2, default=str))
    tmp.replace(path)
    svc.oplog.add("config", "Экспорт настроек", "ok", f"{path} ({', '.join(f'{k}: {v}' for k, v in counts.items())})")
    return counts


def read_backup(path: Path) -> tuple[dict, dict[str, object]]:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "manifest.json" not in names:
                raise ConfigBackupError("Это не резервная копия настроек DB Workspace Pro (нет manifest.json).")
            for info in z.infolist():
                if info.file_size > MAX_ENTRY:
                    raise ConfigBackupError(f"Слишком большой элемент архива: {info.filename}.")
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != FORMAT:
                raise ConfigBackupError("Неизвестный формат файла настроек.")
            if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
                raise ConfigBackupError("Файл создан более новой версией программы — обновите программу.")
            data = {}
            for sec in SECTIONS:
                if f"{sec}.json" in names:
                    data[sec] = json.loads(z.read(f"{sec}.json").decode("utf-8"))
            return manifest, data
    except zipfile.BadZipFile as exc:
        raise ConfigBackupError("Файл повреждён или не является архивом настроек.") from exc
    except FileNotFoundError as exc:
        raise ConfigBackupError(f"Файл не найден: {path}") from exc
    except OSError as exc:
        raise ConfigBackupError(f"Не удалось прочитать файл: {exc.strerror or exc}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConfigBackupError(f"Файл настроек повреждён: {exc}") from exc


def import_config(svc: AppServices, path: Path, sections: set[str] | None = None, *, overwrite: bool = True) -> ImportReport:
    """overwrite=True — объекты с тем же идентификатором заменяются; False — существующие пропускаются."""
    manifest, data = read_backup(Path(path))
    sections = set(sections or SECTIONS) & set(data)
    rep = ImportReport()
    if "settings" in sections:
        for k, v in (data["settings"] or {}).items():
            try:
                if svc.settings.get(k) == v:
                    continue
                svc.settings.set(k, v)
                rep.bump("updated", "settings")
            except (KeyError, ValueError, TypeError):
                rep.bump("skipped", "settings")
                rep.warnings.append(f"Настройка «{k}» пропущена: неизвестна или недопустимое значение.")
    if "profiles" in sections:
        names = set(ConnectionProfile.__dataclass_fields__)
        for d in data["profiles"] or []:
            try:
                p = ConnectionProfile(**{k: v for k, v in d.items() if k in names})
            except TypeError:
                rep.bump("skipped", "profiles")
                continue
            exists = svc.profiles.repo.get(p.id) is not None
            if exists and not overwrite:
                rep.bump("skipped", "profiles")
                continue
            try:
                svc.profiles.repo.save(p)
            except Exception as exc:  # noqa: BLE001
                rep.bump("skipped", "profiles")
                rep.warnings.append(f"Подключение «{p.name}» не импортировано: {exc}")
                continue
            rep.bump("updated" if exists else "added", "profiles")
            if p.auth == "sql":
                rep.warnings.append(f"Подключение «{p.name}»: пароль не переносится — введите его при подключении.")
    if "favorites" in sections:
        have = {(f.title, f.sql) for f in svc.favorites.list()}
        for d in data["favorites"] or []:
            if (d.get("title"), d.get("sql")) in have or not d.get("sql"):
                rep.bump("skipped", "favorites")
                continue
            svc.favorites.add(str(d.get("title") or "Запрос"), str(d["sql"]))
            rep.bump("added", "favorites")
    if "templates" in sections:
        from reports.model import ReportTemplate, validate_template
        store = _templates(svc)
        existing = {i.id for i in store.list()}
        for d in data["templates"] or []:
            try:
                t = ReportTemplate.from_dict(d)
            except Exception:  # noqa: BLE001
                rep.bump("skipped", "templates")
                continue
            if store.is_builtin(t.id) or (t.id in existing and not overwrite):
                rep.bump("skipped", "templates")
                continue
            store.save(t)
            rep.bump("updated" if t.id in existing else "added", "templates")
            if validate_template(t):
                rep.warnings.append(f"Шаблон «{t.name}» импортирован, но содержит ошибки — проверьте его в конструкторе.")
    auto = _automation(svc) if sections & {"scenarios", "tasks"} else None
    if "scenarios" in sections:
        from automation.scripts import Scenario
        names = set(Scenario.__dataclass_fields__)
        for d in data["scenarios"] or []:
            s = Scenario(**{k: v for k, v in d.items() if k in names})
            exists = auto.scenarios.get(s.id) is not None
            if exists and not overwrite:
                rep.bump("skipped", "scenarios")
                continue
            if s.approved_hash:
                s.approved_hash, s.approved_at = "", ""
                rep.warnings.append(f"Сценарий «{s.name}»: подтверждение снято — проверьте SQL и подтвердите заново.")
            auto.scenarios.save(s)
            rep.bump("updated" if exists else "added", "scenarios")
    if "tasks" in sections:
        from automation.schedule import Schedule
        from automation.store import ScheduledTask
        disabled = 0
        for d in data["tasks"] or []:
            try:
                t = ScheduledTask(d["id"], d["name"], d["kind"], Schedule.from_dict(d.get("schedule") or {}),
                                  dict(d.get("config") or {}), False, bool(d.get("catch_up")))
            except (KeyError, TypeError):
                rep.bump("skipped", "tasks")
                continue
            exists = auto.tasks.get(t.id) is not None
            if exists and not overwrite:
                rep.bump("skipped", "tasks")
                continue
            auto.tasks.save(t)                         # выключена; включает пользователь после проверки
            disabled += 1
            rep.bump("updated" if exists else "added", "tasks")
        if disabled:
            rep.warnings.append(f"Задач по расписанию импортировано выключенными: {disabled}. Проверьте их и включите вручную.")
    svc.oplog.add("config", "Импорт настроек", "warning" if rep.warnings else "ok",
                  f"{path} (создан {manifest.get('created', '?')}, версия {manifest.get('app_version', '?')})")
    return rep


def reset_settings(svc: AppServices) -> int:
    """Возвращает настройки к значениям по умолчанию (подключения, шаблоны и задачи не затрагиваются)."""
    from storage.settings import SPECS
    n = 0
    for k, spec in SPECS.items():
        if svc.settings.get(k) != spec.default:
            svc.settings.set(k, spec.default)
            n += 1
    svc.oplog.add("config", "Сброс настроек", "ok", f"изменено значений: {n}")
    return n
