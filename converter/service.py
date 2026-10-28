"""Связка конвертера с приложением: настройки, журнал операций, история."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bootstrap import AppServices
from .engine import BatchSummary, ConvertOptions, Job, JobResult, run_batch
from .formats import FORMATS, Capabilities
from .libreoffice import find_soffice

FILES_PREFIX = "FILES:"
STATUS_TO_LOG = {"ok": "ok", "failed": "error", "skipped": "warning", "unsupported": "warning", "cancelled": "warning"}
STATUS_TEXT = {"ok": "Готово", "failed": "Ошибка", "skipped": "Пропущен", "unsupported": "Не поддерживается",
               "cancelled": "Отменён"}


@dataclass(frozen=True)
class ConversionRecord:
    ts: str
    action: str
    status: str
    files: list[str]
    message: str


def default_output_dir() -> Path:
    return Path.home() / "Documents" / "DB Workspace Pro" / "Конвертация"


class ConverterService:
    def __init__(self, services: AppServices):
        self.svc = services

    def caps(self) -> Capabilities:
        return Capabilities(find_soffice(self.svc.settings.get("libreoffice_path")))

    def options(self, **override) -> ConvertOptions:
        s = self.svc.settings
        out = s.get("convert_output_dir")
        o = ConvertOptions(out_dir=Path(out) if out else default_output_dir(), same_dir=s.get("convert_same_dir"),
                           policy=s.get("convert_policy"), csv_delimiter=s.get("export_csv_delimiter"),
                           formula_guard=s.get("export_formula_guard"), soffice_path=s.get("libreoffice_path"),
                           timeout=float(s.get("convert_timeout")), temp_root=self.svc.paths.temp)
        for k, v in override.items():
            setattr(o, k, v)
        return o

    def workers(self) -> int:
        return self.svc.settings.get("convert_workers")

    def run(self, jobs: list[Job], dst: str, opts: ConvertOptions, *, workers: int | None = None, progress=None,
            on_result=None, cancel_token=None) -> BatchSummary:
        def logged(i: int, r: JobResult) -> None:
            self._log_result(r)
            if on_result:
                on_result(i, r)

        summary = run_batch(jobs, dst, opts, workers=workers or self.workers(), progress=progress, on_result=logged,
                            cancel_token=cancel_token)
        problems = summary.check_consistency()
        status = "ok" if summary.failed == 0 and summary.cancelled == 0 and not problems else \
            ("error" if summary.ok == 0 and summary.total else "warning")
        note = (f"{FORMATS[dst].title}: всего {summary.total}, успешно {summary.ok}, ошибок {summary.failed}, "
                f"пропущено {summary.skipped}, отменено {summary.cancelled}"
                + (f"; ПРОБЛЕМЫ КОНТРОЛЯ: {'; '.join(problems)}" if problems else ""))
        self.svc.oplog.add("conversion", f"Пакет: {summary.total} файл(ов)", status, note, int(summary.elapsed * 1000))
        return summary

    def _log_result(self, r: JobResult) -> None:
        files = [str(p) for p in r.outputs][:10]
        head = FILES_PREFIX + json.dumps(files, ensure_ascii=False) + "\n" if files else ""
        msg = head + (r.error or "; ".join(r.warnings[:2]))
        self.svc.oplog.add("conversion", f"{r.src.name} → {FORMATS[r.dst].title}", STATUS_TO_LOG[r.status],
                           msg.strip(), int(r.elapsed * 1000))

    def recent(self, limit: int = 30) -> list[ConversionRecord]:
        out = []
        for r in self.svc.oplog.recent(limit * 3, "conversion"):
            if r.action.startswith("Пакет:"):
                continue
            files: list[str] = []
            msg = r.message
            if msg.startswith(FILES_PREFIX):
                head, _, msg = msg.partition("\n")
                try:
                    files = json.loads(head[len(FILES_PREFIX):])
                except ValueError:
                    files = []
            out.append(ConversionRecord(r.ts, r.action, r.status, files, msg))
            if len(out) >= limit:
                break
        return out
