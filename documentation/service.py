"""Связка генератора документации с приложением."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from bootstrap import AppServices
from comparison.schema import SchemaModel, read_schema
from comparison.schema_diff import compare_schemas
from reports.adhoc import save_doc

from .generator import DocOptions, build_documentation
from .store import SchemaSnapshotStore


@dataclass
class DocResult:
    files: list[Path] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    model: SchemaModel | None = None
    changes: int | None = None                  # None — сравнивать было не с чем


def default_output_dir() -> Path:
    return Path.home() / "Documents" / "DB Workspace Pro" / "Документация"


class DocumentationService:
    def __init__(self, services: AppServices):
        self.svc = services
        self.snapshots = SchemaSnapshotStore(services.paths.data / "schema_snapshots")

    def output_dir(self) -> Path:
        custom = self.svc.settings.get("doc_output_dir")
        return Path(custom) if custom else default_output_dir()

    def read_model(self, profile_id: str, database: str, *, progress=lambda s, f: None, cancel_token=None) -> SchemaModel:
        profile = self.svc.profiles.repo.get(profile_id)
        server = profile.server_spec() if profile else ""
        with self.svc.profiles.lease(profile_id, "schema") as conn:
            return read_schema(conn, database, server=server, cancel_token=cancel_token, progress=progress)

    def generate(self, profile_id: str, database: str, opts: DocOptions, formats: list[str], *,
                 output_dir: Path | None = None, policy: str = "new_version", progress=lambda s, f: None,
                 cancel_token=None) -> DocResult:
        started = dt.datetime.now()
        model = self.read_model(profile_id, database, progress=progress, cancel_token=cancel_token)
        previous = self.snapshots.latest(profile_id, database) if opts.include_changes else None
        progress("Сборка документа", None)
        doc = build_documentation(model, opts, previous)
        s = self.svc.settings
        progress("Сохранение файлов", None)
        res = save_doc(doc, Path(output_dir) if output_dir else self.output_dir(),
                       f"Документация_{database}_{started:%Y-%m-%d}", formats, policy=policy,
                       soffice_path=s.get("libreoffice_path"), timeout=float(s.get("report_pdf_timeout")),
                       cancel_token=cancel_token, temp_root=self.svc.paths.temp)
        out = DocResult(res.files, res.errors, res.warnings + list(model.warnings), model)
        if previous is not None:
            out.changes = sum(1 for d in compare_schemas(model, previous).items if d.status != "unknown")
        if res.files:
            self.snapshots.save(profile_id, database, model)
        status = "ok" if res.files and not res.errors else ("warning" if res.files else "error")
        self.svc.oplog.add("documentation", f"Документация {database}", status,
                           "; ".join(str(f) for f in res.files) or "; ".join(res.errors.values()),
                           int((dt.datetime.now() - started).total_seconds() * 1000))
        return out
