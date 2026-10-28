"""Формирование отчёта: данные → документ → файлы выбранных форматов.

Принципы: только чтение из БД; отмена в любой момент; если один формат не удался, готовые остальные
сохраняются; файлы пишутся через временную папку и атомарную замену; существующие файлы не
перезаписываются без явного разрешения (политика new_version/overwrite/skip).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import shutil
import tempfile
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from converter.libreoffice import (INSTALL_HINT, ConversionCancelled, ConversionError, convert, find_soffice)
from database.connection import SqlConnection
from database.editor_session import ExecutionListener
from database.errors import DbWorkspaceError, QueryCancelledError
from database.sqltext import quote_ident
from utils.files import atomic_copy, ensure_free_space, sanitize_filename
from .data import Dataset, SourceError, fetch_dataset
from .document import BuildContext, ReportDoc, build_document
from .model import FORMATS, FORMAT_TITLES, ReportTemplate, validate_template
from .render_docx import render_docx
from .render_formats import render_csv, render_html, render_md, render_xlsx
from .variables import ParamError, coerce_params

log = logging.getLogger("dbworkspace.reports")
ORDER = ("docx", "pdf", "xlsx", "html", "md", "csv")
OVERWRITE_POLICIES = ("new_version", "overwrite", "skip")


class ReportError(DbWorkspaceError):
    pass


class ReportCancelled(ReportError):
    def __init__(self, message: str = "Формирование отчёта отменено.", result: "GenerationResult | None" = None):
        super().__init__(message)
        self.result = result


@dataclass
class ProducedFile:
    path: Path
    format: str


@dataclass
class GenerationResult:
    files: list[ProducedFile] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)       # формат -> причина
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)
    parts: int = 1
    datasets: dict = field(default_factory=dict)                 # заполняется при keep_datasets=True
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.files)


@dataclass
class GenerationRequest:
    template: ReportTemplate
    params: dict[str, str]
    formats: list[str]
    output_dir: Path
    connection_factory: Callable[[], AbstractContextManager]    # -> контекст, возвращающий SqlConnection
    base_name: str = ""
    overwrite: str = "new_version"
    max_rows: int = 5000
    batch_size: int = 1000
    server_name: str = ""
    database: str = ""
    csv_delimiter: str = ";"
    formula_guard: bool = True
    soffice_path: str = ""
    pdf_timeout: float = 180.0
    app_version: str = ""
    temp_root: Path | None = None
    generated_at: dt.datetime | None = None
    keep_datasets: bool = False


def family_exists(out: Path, stem: str, fmt: str) -> bool:
    if fmt == "csv":
        return (out / f"{stem}.csv").exists() or any(out.glob(f"{glob_escape(stem)}_[0-9][0-9]_*.csv"))
    if fmt == "md":
        return (out / f"{stem}.md").exists() or (out / f"{stem}_files").exists()
    return (out / f"{stem}.{fmt}").exists()


def glob_escape(s: str) -> str:
    return "".join(f"[{c}]" if c in "*?[" else c for c in s)


def choose_stem(out: Path, stem: str, formats: list[str], policy: str) -> tuple[str, list[str]]:
    """(имя без расширения, форматы, которые нужно пропустить)."""
    if policy == "overwrite":
        return stem, []
    if policy == "skip":
        return stem, [f for f in formats if family_exists(out, stem, f)]
    n, cand = 1, stem
    while any(family_exists(out, cand, f) for f in formats):
        n += 1
        cand = f"{stem} ({n})"
    return cand, []


def default_stem(template: ReportTemplate, when: dt.datetime) -> str:
    return sanitize_filename(f"{template.name}_{when:%Y-%m-%d_%H%M}", "report", 120).replace(" ", "_")


def _check_cancel(token) -> None:
    if token is not None and token.is_cancelled:
        raise ReportCancelled()


def fetch_all(req: GenerationRequest, values: dict, progress, cancel_token) -> tuple[dict[str, Dataset], list[str]]:
    t = req.template
    used = []
    for b in t.blocks:
        if b.source and b.source not in used and t.source(b.source) is not None:
            used.append(b.source)
    datasets: dict[str, Dataset] = {}
    warnings: list[str] = []
    if not used:
        return datasets, warnings
    with req.connection_factory() as conn:
        current_db: list[str | None] = [None]
        if req.database:
            conn.execute_multi("USE " + quote_ident(req.database), listener=ExecutionListener())
            current_db[0] = req.database
        for i, sid in enumerate(used):
            _check_cancel(cancel_token)
            src = t.source(sid)
            progress(f"Получение данных: {src.name or sid}", i / len(used))
            try:
                ds = fetch_dataset(conn, src, values, max_rows=req.max_rows, batch_size=req.batch_size,
                                   cancel_token=cancel_token, current_db=current_db)
            except QueryCancelledError as exc:
                raise ReportCancelled() from exc
            except (SourceError, ParamError, ValueError, DbWorkspaceError) as exc:
                if t.continue_on_error:
                    warnings.append(f"Источник «{src.name or sid}» пропущен: {exc}")
                    continue
                raise ReportError(str(exc)) from exc
            datasets[sid] = ds
            warnings.extend(ds.warnings)
    return datasets, warnings


def count_parts(template: ReportTemplate, datasets: dict[str, Dataset], max_rows: int) -> int:
    if template.part_rows <= 0:
        return 1
    parts = 1
    for b in template.blocks:
        ds = datasets.get(b.source) if b.source else None
        if b.type == "table" and ds is not None and not b.group_by:
            n = len(ds.rows[: (b.max_rows or max_rows)])
            parts = max(parts, math.ceil(n / template.part_rows))
    return parts


def _render(fmt: str, doc: ReportDoc, stage: Path, stem: str, req: GenerationRequest, cancel_token,
            docx_path: Path | None):
    """Возвращает (список файлов в stage, предупреждения)."""
    if fmt == "docx":
        warns = render_docx(doc, stage / f"{stem}.docx")
        return [stage / f"{stem}.docx"], warns
    if fmt == "pdf":
        if docx_path is None or not docx_path.exists():
            raise ReportError("Для PDF нужен DOCX, но он не был сформирован.")
        soffice = find_soffice(req.soffice_path)
        if not soffice:
            raise ConversionError(f"Указанный в настройках путь к LibreOffice не найден: {req.soffice_path}"
                                  if req.soffice_path else INSTALL_HINT)
        pdf = convert(docx_path, "pdf", stage, soffice=soffice, timeout=req.pdf_timeout, cancel_token=cancel_token)
        return [pdf], ["PDF получен из DOCX через LibreOffice: номера страниц в оглавлении не проставляются."]
    opts = {"delimiter": req.csv_delimiter, "formula_guard": req.formula_guard}
    fn = {"html": render_html, "md": render_md, "xlsx": render_xlsx, "csv": render_csv}[fmt]
    res = fn(doc, stage, stem, **opts)
    return res.files, res.warnings


def generate_report(req: GenerationRequest, *, progress: Callable[[str, float | None], None] | None = None,
                    cancel_token=None) -> GenerationResult:
    progress = progress or (lambda stage, frac: None)
    started = time.perf_counter()
    t = req.template
    errors = validate_template(t)
    if errors:
        raise ReportError("Шаблон содержит ошибки:\n• " + "\n• ".join(errors))
    formats = [f for f in ORDER if f in req.formats]
    if not formats:
        raise ReportError("Не выбран ни один формат.")
    if any(f not in FORMATS for f in req.formats):
        raise ReportError("Неизвестный формат вывода.")
    if req.overwrite not in OVERWRITE_POLICIES:
        raise ReportError("Неизвестная политика перезаписи.")
    out = Path(req.output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        ensure_free_space(out, 0)
    except OSError as exc:
        raise ReportError(f"Папка для отчёта недоступна: {exc.strerror or exc}") from exc
    try:
        values = coerce_params(t.params, req.params)
    except ParamError as exc:
        raise ReportError(str(exc)) from exc

    result = GenerationResult()
    when = req.generated_at or dt.datetime.now()
    try:
        datasets, warns = fetch_all(req, values, progress, cancel_token)
    except ReportCancelled:
        raise
    result.warnings.extend(warns)
    result.rows = {sid: len(ds.rows) for sid, ds in datasets.items()}
    if req.keep_datasets:
        result.datasets = datasets
    parts = count_parts(t, datasets, req.max_rows)
    result.parts = parts
    base = sanitize_filename(req.base_name, "report", 120) if req.base_name else default_stem(t, when)
    stage_root = Path(tempfile.mkdtemp(prefix="dbwp-report-", dir=req.temp_root))
    try:
        for part in range(parts):
            _check_cancel(cancel_token)
            progress("Подготовка документа" + (f" (часть {part + 1} из {parts})" if parts > 1 else ""), None)
            ctx = BuildContext(t, datasets, values, dict(req.params), when, req.server_name, req.database,
                               req.max_rows, (part, parts) if parts > 1 else None, req.app_version)
            doc = build_document(ctx)
            if part == 0:
                result.warnings.extend(doc.warnings)
            stem0 = base if parts == 1 else f"{base}_part{part + 1:02d}"
            stem, skip = choose_stem(out, stem0, formats, req.overwrite)
            todo = [f for f in formats if f not in skip]
            for f in skip:
                result.skipped.append(f"{FORMAT_TITLES[f]}: файл «{stem}» уже существует — пропущено")
            stage = stage_root / f"p{part}"
            stage.mkdir()
            docx_stage: Path | None = None
            if "pdf" in todo and "docx" not in todo:
                todo = ["docx_tmp"] + todo
            for i, fmt in enumerate(todo):
                _check_cancel(cancel_token)
                real = "docx" if fmt == "docx_tmp" else fmt
                label = FORMAT_TITLES[real] + (" (промежуточный)" if fmt == "docx_tmp" else "")
                progress(f"Формирование: {label}", i / max(1, len(todo)))
                try:
                    files, warns = _render(real, doc, stage, stem, req, cancel_token, docx_stage)
                    if real == "docx":
                        docx_stage = files[0]
                    result.warnings.extend(warns)
                    if fmt == "docx_tmp":
                        continue
                    for f in files:
                        dest = out / f.relative_to(stage)
                        ensure_free_space(dest.parent, f.stat().st_size)
                        atomic_copy(f, dest)
                    main = out / files[0].relative_to(stage)
                    for f in files if real in ("csv",) else files[:1]:
                        result.files.append(ProducedFile(out / f.relative_to(stage), real))
                except (ReportCancelled, ConversionCancelled, QueryCancelledError):
                    raise ReportCancelled(result=result)
                except Exception as exc:  # noqa: BLE001 — сбой одного формата не отменяет остальные
                    log.exception("Не удалось сформировать %s", real)
                    msg = str(exc) if isinstance(exc, (ReportError, ConversionError, OSError)) else \
                        f"{exc.__class__.__name__}: {exc}"
                    result.errors[real] = msg
    except ReportCancelled as exc:
        exc.result = exc.result or result
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    result.elapsed = time.perf_counter() - started
    progress("Готово", 1.0)
    return result
