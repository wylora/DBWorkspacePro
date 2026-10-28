"""Связка отчётов с приложением: настройки, соединения, журнал операций, недавние отчёты."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from bootstrap import AppServices
from converter.libreoffice import find_soffice
from version import __version__
from .generator import GenerationRequest, GenerationResult, ReportCancelled, ReportError, generate_report
from .model import FORMATS, ReportTemplate
from .templates import TemplateStore

log = logging.getLogger("dbworkspace.reports")
FILES_PREFIX = "FILES:"


@dataclass(frozen=True)
class RecentReport:
    ts: str
    name: str
    status: str
    files: list[str]


def default_output_dir() -> Path:
    return Path.home() / "Documents" / "DB Workspace Pro" / "Отчёты"


class ReportService:
    def __init__(self, services: AppServices):
        self.svc = services
        self.templates = TemplateStore(services.paths.data / "templates")

    # ---- настройки ---------------------------------------------------
    def output_dir(self) -> Path:
        custom = self.svc.settings.get("report_output_dir")
        return Path(custom) if custom else default_output_dir()

    def saved_formats(self) -> list[str]:
        raw = self.svc.settings.get("report_formats")
        out = [f for f in raw.split(",") if f in FORMATS]
        return out or ["docx"]

    def remember_formats(self, formats: list[str]) -> None:
        self.svc.settings.set("report_formats", ",".join(f for f in formats if f in FORMATS) or "docx")

    def soffice(self) -> str | None:
        return find_soffice(self.svc.settings.get("libreoffice_path"))

    # ---- запрос ------------------------------------------------------
    def build_request(self, template: ReportTemplate, params: dict[str, str], formats: list[str], profile_id: str,
                      database: str = "", *, output_dir: Path | str | None = None, base_name: str = "",
                      overwrite: str | None = None, max_rows: int | None = None) -> GenerationRequest:
        profile = self.svc.profiles.repo.get(profile_id)
        if profile is None:
            raise ReportError("Профиль подключения не найден (возможно, удалён).")
        s = self.svc.settings
        profiles = self.svc.profiles
        return GenerationRequest(
            template=template, params=params, formats=formats,
            output_dir=Path(output_dir) if output_dir else self.output_dir(),
            connection_factory=lambda: profiles.lease(profile_id, "report"),
            base_name=base_name, overwrite=overwrite or s.get("report_overwrite"),
            max_rows=max_rows or s.get("report_max_rows"), batch_size=s.get("fetch_batch_size"),
            server_name=profile.server_spec(), database=database,
            csv_delimiter=s.get("export_csv_delimiter"), formula_guard=s.get("export_formula_guard"),
            soffice_path=s.get("libreoffice_path"), pdf_timeout=float(s.get("report_pdf_timeout")),
            app_version=__version__, temp_root=self.svc.paths.temp)

    # ---- выполнение и журнал ------------------------------------------
    def run(self, req: GenerationRequest, *, progress=None, cancel_token=None) -> GenerationResult:
        started = time.time()
        name = req.template.name
        try:
            result = generate_report(req, progress=progress, cancel_token=cancel_token)
        except ReportCancelled as exc:
            self._log(name, "warning", exc.result, "отменено пользователем", started)
            raise
        except Exception as exc:
            self._log(name, "error", None, str(exc), started)
            raise
        if result.ok and not result.errors:
            status, note = "ok", ""
        elif result.ok:
            status, note = "warning", "часть форматов не сформирована"
        else:
            status, note = ("warning", "все файлы пропущены (уже существуют)") if result.skipped else ("error", "файлы не созданы")
        self._log(name, status, result, note, started)
        return result

    def _log(self, name: str, status: str, result: GenerationResult | None, note: str, started: float) -> None:
        files = [str(f.path) for f in result.files][:12] if result else []
        head = FILES_PREFIX + json.dumps(files, ensure_ascii=False) + "\n" if files else ""
        errs = "; ".join(f"{k}: {v}" for k, v in (result.errors.items() if result else []))
        msg = head + " ".join(x for x in (note, ("ОШИБКИ: " + errs) if errs else "") if x)
        self.svc.oplog.add("report", name, status, msg.strip(), int((time.time() - started) * 1000))

    def recent(self, limit: int = 10) -> list[RecentReport]:
        out = []
        for r in self.svc.oplog.recent(300, "report"):
            if r.status not in ("ok", "warning") or not r.message.startswith(FILES_PREFIX):
                continue
            try:
                files = json.loads(r.message[len(FILES_PREFIX):].split("\n", 1)[0])
            except ValueError:
                continue
            out.append(RecentReport(r.ts, r.action, r.status, [f for f in files if isinstance(f, str)]))
            if len(out) >= limit:
                break
        return out
