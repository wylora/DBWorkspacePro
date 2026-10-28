"""Документы, собранные программно (отчёты о сравнении, документация БД), через общие рендереры отчётов."""
from __future__ import annotations

import datetime as dt
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from utils.files import atomic_copy, ensure_free_space, sanitize_filename
from .document import Cell, DocColumn, DocPageBreak, DocTable, DocTitlePage, DocToc, ReportDoc
from .formatting import Formatter, clean_xml_text
from .generator import ORDER, ReportCancelled, _render, choose_stem
from .model import FORMAT_TITLES, ReportTemplate
from .styles import get_style


def table(title: str, headers: list[str], rows: list[list], *, aligns: list[str] | None = None, compact: bool = False,
          note: str = "", fmt: Formatter | None = None) -> DocTable:
    fmt = fmt or Formatter("ru")
    aligns = aligns or ["left"] * len(headers)
    cells = []
    for r in rows:
        line = []
        for i, v in enumerate(r):
            text = v if isinstance(v, str) else fmt.value(v)
            line.append(Cell(clean_xml_text(text), v, aligns[i], v is None or v == ""))
        cells.append(line)
    cols = []
    for i, h in enumerate(headers):
        texts = sorted(len(r[i].text) for r in cells) or [0]
        p90 = texts[min(len(texts) - 1, int(len(texts) * 0.9))]
        cols.append(DocColumn(h, aligns[i], max(4, min(45, max(len(h), p90)))))
    return DocTable(clean_xml_text(title), cols, cells, note=note, compact=compact)


def make_doc(title: str, elements: list, *, style: str = "technical", subtitle: str = "", author: str = "",
             organization: str = "", toc: bool = True, title_page: bool = True, header: str = "",
             orientation: str = "portrait") -> ReportDoc:
    t = ReportTemplate(name=title, title=title, style=style, title_page=title_page, toc=toc, orientation=orientation,
                       author=author, organization=organization, header_text=header or title)
    now = dt.datetime.now()
    fmt = Formatter("ru")
    els: list = []
    if title_page:
        els += [DocTitlePage(title, subtitle, organization, author, fmt.datetime(now)), DocPageBreak()]
    if toc:
        els.append(DocToc())
    els += elements
    return ReportDoc(t, get_style(style), title, header or title, "", author, organization, now, els, [], None, fmt, {})


@dataclass
class AdhocResult:
    files: list[Path] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def save_doc(doc: ReportDoc, folder: Path, stem: str, formats: list[str], *, policy: str = "new_version",
             soffice_path: str = "", timeout: float = 180.0, csv_delimiter: str = ";", formula_guard: bool = True,
             cancel_token=None, temp_root: Path | None = None) -> AdhocResult:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    formats = [f for f in ORDER if f in formats]
    stem, skip = choose_stem(folder, sanitize_filename(stem, "document", 120), formats, policy)
    res = AdhocResult()
    res.warnings += [f"{FORMAT_TITLES[f]}: файл уже существует — пропущено" for f in skip]
    todo = [f for f in formats if f not in skip]
    if "pdf" in todo and "docx" not in todo:
        todo = ["docx_tmp"] + todo
    req = SimpleNamespace(soffice_path=soffice_path, pdf_timeout=timeout, csv_delimiter=csv_delimiter,
                          formula_guard=formula_guard)
    stage = Path(tempfile.mkdtemp(prefix="dbwp-doc-", dir=temp_root))
    docx_stage = None
    try:
        for fmt in todo:
            if cancel_token is not None and cancel_token.is_cancelled:
                raise ReportCancelled()
            real = "docx" if fmt == "docx_tmp" else fmt
            try:
                files, warns = _render(real, doc, stage, stem, req, cancel_token, docx_stage)
                if real == "docx":
                    docx_stage = files[0]
                res.warnings += warns
                if fmt == "docx_tmp":
                    continue
                for f in files:
                    dest = folder / f.relative_to(stage)
                    ensure_free_space(dest.parent, f.stat().st_size)
                    atomic_copy(f, dest)
                res.files.append(folder / files[0].relative_to(stage))
            except ReportCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                res.errors[real] = str(exc)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return res
