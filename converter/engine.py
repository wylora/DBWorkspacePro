"""Ядро конвертера: один файл и пакеты. Исходные файлы никогда не изменяются."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from utils.files import atomic_copy, ensure_free_space, read_text_auto, sanitize_filename
from . import docmodel as dm
from .formats import (EXT_TO_FMT, FORMATS, LO_INFILTER, LO_TARGET_SPEC, PRIMARY_EXT, Route, explain_unavailable, plan,
                      resolve_source)
from .libreoffice import INSTALL_HINT, ConversionCancelled, ConversionError, convert, find_soffice
from .tables import MemReader, TableError, open_reader, write_tables
from .verify import VerifyError, verify_output

log = logging.getLogger("dbworkspace.converter")
POLICIES = ("new_version", "overwrite", "skip")
MAX_FILES = 5000


@dataclass
class ConvertOptions:
    out_dir: Path | None = None
    same_dir: bool = False
    policy: str = "new_version"
    csv_delimiter: str = ";"
    formula_guard: bool = True
    soffice_path: str = ""
    timeout: float = 300.0
    temp_root: Path | None = None
    max_json_mb: int = 200


@dataclass(frozen=True)
class Job:
    src: Path
    rel_dir: Path = Path(".")


@dataclass
class JobResult:
    src: Path
    dst: str
    status: str = "failed"          # ok | failed | skipped | unsupported | cancelled
    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    route: str = ""


@dataclass
class BatchSummary:
    results: list[JobResult]
    elapsed: float = 0.0

    def count(self, *statuses: str) -> int:
        return sum(1 for r in self.results if r.status in statuses)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> int:
        return self.count("ok")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def skipped(self) -> int:
        return self.count("skipped", "unsupported")

    @property
    def cancelled(self) -> int:
        return self.count("cancelled")

    def check_consistency(self) -> list[str]:
        """Контроль: число итогов совпадает с числом файлов, а у каждой успешной операции есть выходные файлы."""
        problems = []
        if self.ok + self.failed + self.skipped + self.cancelled != self.total:
            problems.append("Число результатов не совпадает с числом файлов.")
        for r in self.results:
            if r.status == "ok" and (not r.outputs or any(not p.is_file() or p.stat().st_size == 0 for p in r.outputs)):
                problems.append(f"{r.src.name}: результат отмечен успешным, но выходной файл отсутствует или пуст.")
        return problems


# ================================================================== сбор заданий
def expand_paths(paths: list[Path], recursive: bool = True, max_files: int = MAX_FILES
                 ) -> tuple[list[Job], list[tuple[Path, str]], bool]:
    """Файлы и папки -> задания. Скрытые и временные файлы Office (~$…) пропускаются, ссылки на папки не раскрываются."""
    jobs: list[Job] = []
    ignored: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    truncated = False

    def add(path: Path, rel: Path) -> bool:
        nonlocal truncated
        try:
            key = path.resolve()
        except OSError:
            return True
        if key in seen:
            return True
        name = path.name
        if name.startswith(("~$", ".")):
            return True
        if EXT_TO_FMT.get(path.suffix.lower()) is None:
            ignored.append((path, "формат не поддерживается"))
            return True
        if len(jobs) >= max_files:
            truncated = True
            return False
        seen.add(key)
        jobs.append(Job(path, rel))
        return True

    for p in paths:
        p = Path(p)
        if p.is_dir():
            for root, dirs, files in os.walk(p, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if recursive else []
                rel = Path(os.path.relpath(root, p))
                for f in sorted(files):
                    if not add(Path(root) / f, rel):
                        return jobs, ignored, truncated
        elif p.is_file():
            if not add(p, Path(".")):
                break
        else:
            ignored.append((p, "файл не найден"))
    return jobs, ignored, truncated


# ============================================================== имена результатов
def _family_exists(out: Path, stem: str, dst: str) -> bool:
    ext = PRIMARY_EXT[dst]
    if (out / f"{stem}{ext}").exists():
        return True
    if FORMATS[dst].kind == "table":
        return any(out.glob(f"{_glob_escape(stem)}_*{ext}"))
    return False


def _glob_escape(s: str) -> str:
    return "".join(f"[{c}]" if c in "*?[" else c for c in s)


def choose_output_stem(out: Path, base: str, dst: str, policy: str, src: Path) -> tuple[str, bool]:
    """(имя без расширения, пропустить?). Исходный файл защищён от перезаписи при любой политике."""
    src_key = src.resolve()
    ext = PRIMARY_EXT[dst]

    def collides(stem: str) -> bool:
        return (out / f"{stem}{ext}").resolve() == src_key or (policy != "overwrite" and _family_exists(out, stem, dst))

    if policy == "skip":
        return base, collides(base)
    stem, n = base, 1
    while collides(stem):
        n += 1
        stem = f"{base} ({n})"
    return stem, False


# =================================================================== маршруты
def _read_blocks(fmt: str, path: Path) -> dm.ReadResult:
    if fmt == "docx":
        return dm.read_docx(path)
    text, _enc = read_text_auto(path, max_bytes=100 * 1048576)
    return {"md": dm.parse_markdown, "html": dm.parse_html, "txt": dm.parse_text}[fmt](text)


def _write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _python_route(src_fmt: str, dst: str, src: Path, stem: str, stage: Path, opts: ConvertOptions, warns: list[str],
                  token) -> list[Path]:
    if FORMATS[dst].kind == "table":
        if FORMATS[src_fmt].kind == "table":
            reader = open_reader(src, src_fmt, max_json_mb=opts.max_json_mb)
        else:
            blocks = _read_blocks(src_fmt, src)
            warns += blocks.warnings
            tables = dm.extract_tables(blocks.blocks)
            if not tables:
                raise TableError("В документе нет таблиц для извлечения.")
            reader = MemReader(tables)
        with reader:
            warns += reader.warnings
            res = write_tables(reader, dst, stage, stem, delimiter=opts.csv_delimiter, formula_guard=opts.formula_guard,
                               infer=(src_fmt in ("csv", "tsv") and dst == "xlsx"), cancel_token=token)
        warns += res.warnings
        return res.files
    rr = _read_blocks(src_fmt, src)
    warns += rr.warnings
    if not rr.blocks:
        raise ConversionError("В исходном документе нет содержимого, которое можно перенести.")
    out = stage / f"{stem}{PRIMARY_EXT[dst]}"
    title = src.stem
    if dst == "md":
        _write_text(out, dm.to_markdown(rr.blocks))
    elif dst == "txt":
        _write_text(out, dm.to_text(rr.blocks))
    elif dst == "html":
        _write_text(out, dm.to_html(rr.blocks, title))
    elif dst == "docx":
        dm.to_docx(rr.blocks, out, title)
    else:
        raise ConversionError(f"Внутренний преобразователь не пишет формат {dst}.")
    return [out]


def _lo_route(src_fmt: str, dst: str, src: Path, stem: str, stage: Path, opts: ConvertOptions, soffice: str,
              token) -> list[Path]:
    stage_in, stage_out = stage / "in", stage / "out"
    stage_in.mkdir(exist_ok=True)
    stage_out.mkdir(exist_ok=True)
    copy = stage_in / f"{stem}{PRIMARY_EXT[src_fmt]}"
    if src_fmt == "txt":
        text, _ = read_text_auto(src, max_bytes=200 * 1048576)
        copy.write_text(text, encoding="utf-8", newline="\n")             # LibreOffice получает UTF-8
    else:
        shutil.copyfile(src, copy)
    convert(copy, LO_TARGET_SPEC[dst], stage_out, soffice=soffice, timeout=opts.timeout, cancel_token=token,
            infilter=LO_INFILTER.get(src_fmt))
    files = sorted(p for p in stage_out.iterdir() if p.is_file())
    main = stage_out / f"{stem}{PRIMARY_EXT[dst]}"
    if main not in files:
        raise ConversionError("LibreOffice не создал ожидаемый файл.")
    return [main] + [p for p in files if p != main]


def _run_route(route: Route, src_fmt: str, src: Path, stem: str, stage: Path, opts: ConvertOptions,
               soffice: str | None, warns: list[str], token) -> list[Path]:
    if route.kind == "copy":
        text, _ = read_text_auto(src, max_bytes=200 * 1048576)
        return [_write_text(stage / f"{stem}{PRIMARY_EXT[route.dst]}", text.replace("\r\n", "\n"))]
    if route.kind == "python":
        return _python_route(src_fmt, route.dst, src, stem, stage, opts, warns, token)
    if route.kind == "libreoffice":
        return _lo_route(src_fmt, route.dst, src, stem, stage, opts, soffice, token)
    # цепочка: src -> via -> dst
    mid_dir = stage / "mid"
    mid_dir.mkdir(exist_ok=True)
    r1, r2 = plan(src_fmt, route.via), plan(route.via, route.dst)
    if r1 is None or r2 is None:
        raise ConversionError("Маршрут не найден.")
    first = _run_route(r1, src_fmt, src, stem, mid_dir, opts, soffice, warns, token)
    return _run_route(r2, route.via, first[0], stem, stage, opts, soffice, warns, token)


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, (ConversionError, TableError, VerifyError)):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "Нет доступа к файлу или папке."
    if isinstance(exc, OSError):
        return f"Ошибка файловой системы: {exc.strerror or exc}"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{exc.__class__.__name__}: {exc}"


# ============================================================== один файл
def convert_file(job: Job, dst: str, opts: ConvertOptions, cancel_token=None) -> JobResult:
    started = time.perf_counter()
    res = JobResult(job.src, dst)
    stage: Path | None = None
    try:
        if cancel_token is not None and cancel_token.is_cancelled:
            raise ConversionCancelled("Операция отменена.")
        if not job.src.is_file():
            res.error = "Файл не найден."
            return res
        fmt, notes, err = resolve_source(job.src)
        if fmt is None:
            res.status, res.error = ("unsupported" if err.startswith("Формат") else "failed"), err
            return res
        route = plan(fmt, dst)
        if route is None:
            res.status, res.error = "unsupported", explain_unavailable(fmt, dst)
            return res
        res.route = route.description
        soffice = None
        if route.needs_lo:
            soffice = find_soffice(opts.soffice_path)
            if not soffice:
                res.error = (f"Указанный путь к LibreOffice не найден: {opts.soffice_path}" if opts.soffice_path
                             else INSTALL_HINT)
                return res
        base_out = job.src.parent if opts.same_dir else (opts.out_dir or job.src.parent)
        out = base_out if opts.same_dir else base_out / job.rel_dir
        out.mkdir(parents=True, exist_ok=True)
        stem, skip = choose_output_stem(out, sanitize_filename(job.src.stem, "file", 120), dst, opts.policy, job.src)
        if skip:
            res.status, res.error = "skipped", f"Результат «{stem}{PRIMARY_EXT[dst]}» уже существует."
            return res
        ensure_free_space(out, job.src.stat().st_size * 3)
        stage = Path(tempfile.mkdtemp(prefix="dbwp-conv-", dir=opts.temp_root))
        warns = list(notes)
        files = _run_route(route, fmt, job.src, stem, stage, opts, soffice, warns, cancel_token)
        if cancel_token is not None and cancel_token.is_cancelled:
            raise ConversionCancelled("Операция отменена.")
        main_ext = PRIMARY_EXT[dst]
        for f in files:
            if f.suffix.lower() == main_ext:
                warns += verify_output(f, dst)
            elif not f.is_file():
                raise VerifyError(f"Не создан файл {f.name}.")
        ensure_free_space(out, sum(f.stat().st_size for f in files))
        final: list[Path] = []
        created: list[Path] = []
        try:
            for f in files:
                dest = out / f.name
                existed = dest.exists()
                atomic_copy(f, dest)
                final.append(dest)
                if not existed:
                    created.append(dest)
        except BaseException:
            for p in created:                      # не оставляем частичный результат
                try:
                    p.unlink()
                except OSError:
                    pass
            raise
        res.outputs = final
        res.warnings = list(dict.fromkeys(warns + list(route.notes)))
        res.status = "ok"
    except ConversionCancelled:
        res.status, res.error = "cancelled", "Операция отменена."
    except Exception as exc:  # noqa: BLE001 — ошибка одного файла не должна останавливать пакет
        if not isinstance(exc, (ConversionError, TableError, VerifyError, OSError, ValueError)):
            log.exception("Сбой преобразования %s", job.src)
        res.status, res.error = "failed", _friendly(exc)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        res.elapsed = time.perf_counter() - started
    return res


# ================================================================ пакет
def run_batch(jobs: list[Job], dst: str, opts: ConvertOptions, *, workers: int = 1,
              progress: Callable[[str, float], None] | None = None,
              on_result: Callable[[int, JobResult], None] | None = None, cancel_token=None) -> BatchSummary:
    started = time.perf_counter()
    total = len(jobs)
    results: list[JobResult | None] = [None] * total
    done = [0]
    lock = threading.Lock()

    def work(i: int) -> None:
        job = jobs[i]
        if cancel_token is not None and cancel_token.is_cancelled:
            r = JobResult(job.src, dst, "cancelled", error="Не обработан: операция отменена.")
        else:
            if progress:
                with lock:
                    frac = done[0] / max(1, total)
                progress(f"Файл {i + 1} из {total}: {job.src.name}", frac)
            r = convert_file(job, dst, opts, cancel_token)
        results[i] = r
        with lock:
            done[0] += 1
        if on_result:
            on_result(i, r)

    if workers <= 1 or total <= 1:
        for i in range(total):
            work(i)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, total), thread_name_prefix="dbwp-conv") as ex:
            list(ex.map(work, range(total)))
    if progress:
        progress("Готово", 1.0)
    return BatchSummary([r for r in results if r is not None], time.perf_counter() - started)
