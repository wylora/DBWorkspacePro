"""Потоковый экспорт таблиц в CSV, JSON и XLSX.

* Запись через временный файл и атомарное переименование; при ошибке/отмене временный файл удаляется.
* XLSX: режим write_only (данные не накапливаются в памяти), автоматическое разбиение на листы
  при превышении лимита Excel (1 048 576 строк), защита от формул, очистка недопустимых символов.
* CSV: BOM utf-8 (чтобы Excel верно прочитал кириллицу), выбор разделителя, защита от формул.
"""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .files import InsufficientDiskSpaceError, ensure_free_space
from .formatting import format_cell

XLSX_MAX_ROWS = 1_048_576          # включая строку заголовка
XLSX_MAX_COLUMNS = 16_384
XLSX_MAX_CELL_CHARS = 32_767
XLSX_SAFE_INT = 10 ** 15           # Excel хранит числа как double (~15 значащих цифр)
FORMATS = ("csv", "json", "xlsx")
EXTENSIONS = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}
_NUMERIC_TEXT = re.compile(r"^[+-]?\d+([.,]\d+)?$")


class ExportError(Exception):
    pass


@dataclass
class ExportResult:
    path: Path
    rows: int = 0
    sheets: int = 1
    warnings: list[str] = field(default_factory=list)


def guard_formula(text: str) -> str:
    """Префикс ' для текста, который Excel мог бы выполнить как формулу (CSV-инъекция)."""
    if not text:
        return text
    first = text[0]
    if first in ("=", "@", "\t", "\r") or (first in "+-" and not _NUMERIC_TEXT.match(text)):
        return "'" + text
    return text


def export_text(value, guard: bool = False) -> str:
    """Текстовое представление значения для CSV/буфера обмена (без усечения)."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex().upper()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return guard_formula(value) if guard else value
    text = format_cell(value, max_len=10 ** 9)
    return text


def json_value(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex().upper()
    if isinstance(value, uuid.UUID):
        return str(value).upper()
    return str(value)


def unique_names(columns: Sequence[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, c in enumerate(columns):
        name = c or f"column{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


class _Writer:
    def __init__(self, path: str | os.PathLike, columns: Sequence[str], estimated_bytes: int = 0):
        self.target = Path(path)
        self.columns = list(columns)
        self.result = ExportResult(self.target)
        try:
            self.target.parent.mkdir(parents=True, exist_ok=True)
            ensure_free_space(self.target.parent, estimated_bytes)
            fd, tmp = tempfile.mkstemp(prefix=self.target.name + ".", suffix=".tmp", dir=self.target.parent)
        except InsufficientDiskSpaceError as exc:
            raise ExportError(str(exc)) from exc
        except OSError as exc:
            raise ExportError(f"Не удалось создать файл: {exc.strerror or exc}") from exc
        self._fd, self.tmp = fd, Path(tmp)
        self._closed = False

    def _finish(self) -> ExportResult:
        try:
            os.replace(self.tmp, self.target)
        except OSError as exc:
            self.abort()
            raise ExportError(f"Не удалось сохранить файл: {exc.strerror or exc}") from exc
        self._closed = True
        return self.result

    def abort(self) -> None:
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass
        try:
            self.tmp.unlink()
        except OSError:
            pass


class CsvWriter(_Writer):
    def __init__(self, path, columns, *, delimiter: str = ";", formula_guard: bool = True, estimated_bytes: int = 0):
        super().__init__(path, columns, estimated_bytes)
        if len(delimiter) != 1:
            self.abort()
            raise ExportError("Разделитель CSV должен быть одним символом")
        self._guard = formula_guard
        self._fh = os.fdopen(self._fd, "w", newline="", encoding="utf-8-sig")
        self._csv = csv.writer(self._fh, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        self._guarded = 0
        self._write(self._csv.writerow, [export_text(c, self._guard) for c in self.columns])

    def _write(self, fn, *a):
        try:
            fn(*a)
        except OSError as exc:
            self.abort()
            raise ExportError(f"Ошибка записи файла: {exc.strerror or exc}") from exc

    def write_rows(self, rows: Iterable[Sequence]) -> None:
        for row in rows:
            cells = []
            for v in row:
                t = export_text(v, self._guard)
                if self._guard and isinstance(v, str) and t is not v and t != v:
                    self._guarded += 1
                cells.append(t)
            self._write(self._csv.writerow, cells)
            self.result.rows += 1

    def close(self) -> ExportResult:
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        except OSError as exc:
            self.abort()
            raise ExportError(f"Ошибка записи файла: {exc.strerror or exc}") from exc
        if self._guarded:
            self.result.warnings.append(
                f"{self._guarded} знач. начинались с =, +, -, @ и получили префикс ' (защита от формул Excel)")
        return self._finish()

    def abort(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        super().abort()


class JsonWriter(_Writer):
    def __init__(self, path, columns, *, estimated_bytes: int = 0, **_ignored):
        super().__init__(path, columns, estimated_bytes)
        self.keys = unique_names(self.columns)
        if self.keys != self.columns:
            self.result.warnings.append("Повторяющиеся имена столбцов переименованы (_2, _3…)")
        self._fh = os.fdopen(self._fd, "w", encoding="utf-8", newline="\n")
        self._first = True
        self._write("[\n")

    def _write(self, text: str) -> None:
        try:
            self._fh.write(text)
        except OSError as exc:
            self.abort()
            raise ExportError(f"Ошибка записи файла: {exc.strerror or exc}") from exc

    def write_rows(self, rows: Iterable[Sequence]) -> None:
        for row in rows:
            obj = {k: json_value(v) for k, v in zip(self.keys, row)}
            self._write(("" if self._first else ",\n") + json.dumps(obj, ensure_ascii=False))
            self._first = False
            self.result.rows += 1

    def close(self) -> ExportResult:
        self._write("\n]\n")
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        except OSError as exc:
            self.abort()
            raise ExportError(f"Ошибка записи файла: {exc.strerror or exc}") from exc
        return self._finish()

    def abort(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        super().abort()


class XlsxWriter(_Writer):
    def __init__(self, path, columns, *, sheet_name: str = "Результат", formula_guard: bool = True,
                 max_rows_per_sheet: int = XLSX_MAX_ROWS - 1, estimated_bytes: int = 0, **_ignored):
        if len(columns) > XLSX_MAX_COLUMNS:
            raise ExportError(f"В Excel не больше {XLSX_MAX_COLUMNS} столбцов (в результате {len(columns)}). "
                              "Выгрузите в CSV или JSON либо уменьшите число столбцов.")
        super().__init__(path, columns, estimated_bytes)
        try:
            from openpyxl import Workbook
            from openpyxl.cell import WriteOnlyCell
            from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
            from openpyxl.styles import Font
        except ImportError as exc:
            self.abort()
            raise ExportError("Для экспорта в Excel нужна библиотека openpyxl (pip install openpyxl).") from exc
        os.close(self._fd)  # openpyxl сам откроет путь
        self._fd = os.open(self.tmp, os.O_RDONLY)  # держим дескриптор для единообразия abort()
        self._cell, self._font, self._bad = WriteOnlyCell, Font(bold=True), ILLEGAL_CHARACTERS_RE
        self._wb = Workbook(write_only=True)
        self._base = re.sub(r"[\[\]:*?/\\]", "_", sheet_name)[:25] or "Лист"
        self._max = max(1, max_rows_per_sheet)
        self._sheet_rows = 0
        self._guard = formula_guard
        self._truncated = self._bigint = self._guarded = 0
        self._used_titles: set[str] = set()
        self._new_sheet()

    def _unique_title(self, base: str) -> str:
        title, n = base, 1
        while title.lower() in self._used_titles:
            n += 1
            title = f"{base} ({n})"
        self._used_titles.add(title.lower())
        return title

    def begin_sheet(self, name: str, columns: Sequence[str]) -> None:
        """Начинает новый лист с собственным именем и заголовками (для многолистовых книг)."""
        if len(columns) > XLSX_MAX_COLUMNS:
            raise ExportError(f"В Excel не больше {XLSX_MAX_COLUMNS} столбцов (в листе {len(columns)}).")
        self.columns = list(columns)
        self._base = re.sub(r"[\[\]:*?/\\]", "_", name).strip().strip("'")[:25] or "Лист"
        self._new_sheet()

    def _new_sheet(self) -> None:
        self._ws = self._wb.create_sheet(self._unique_title(self._base))
        header = []
        for c in self.columns:
            cell = self._cell(self._ws, value=self._text(str(c) if c is not None else ""))
            cell.data_type = "s"
            cell.font = self._font
            header.append(cell)
        self._ws.append(header)
        self._sheet_rows = 0
        self.result.sheets = len(self._wb.worksheets)

    def _text(self, s: str) -> str:
        s = self._bad.sub("", s)
        if len(s) > XLSX_MAX_CELL_CHARS:
            self._truncated += 1
            s = s[:XLSX_MAX_CELL_CHARS]
        return s

    def _convert(self, v):
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, int):
            if abs(v) >= XLSX_SAFE_INT:
                self._bigint += 1
                return str(v)
            return v
        if isinstance(v, str):
            s = self._text(v)
            if s.startswith(("=", "+", "-", "@")):
                cell = self._cell(self._ws, value=s)
                cell.data_type = "s"       # всегда текст, формула не выполняется
                if self._guard:
                    self._guarded += 1
                return cell
            return s
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        if isinstance(v, decimal.Decimal):
            return float(v) if abs(v) < XLSX_SAFE_INT else str(v)
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        if isinstance(v, (dt.date, dt.time)):
            return v.replace(tzinfo=None) if isinstance(v, dt.time) else v
        if isinstance(v, (bytes, bytearray, memoryview)):
            return "0x" + bytes(v).hex().upper()
        return self._text(str(v))

    def write_rows(self, rows: Iterable[Sequence]) -> None:
        for row in rows:
            if self._sheet_rows >= self._max:
                self._new_sheet()
            self._ws.append([self._convert(v) for v in row])
            self._sheet_rows += 1
            self.result.rows += 1

    def close(self) -> ExportResult:
        try:
            self._wb.save(self.tmp)
        except OSError as exc:
            self.abort()
            raise ExportError(f"Ошибка записи файла: {exc.strerror or exc}") from exc
        try:
            os.close(self._fd)
        except OSError:
            pass
        r = self.result
        if r.sheets > 1:
            r.warnings.append(f"Данные не поместились на один лист Excel: создано листов — {r.sheets}")
        if self._truncated:
            r.warnings.append(f"{self._truncated} знач. длиннее {XLSX_MAX_CELL_CHARS} символов усечены (лимит Excel)")
        if self._bigint:
            r.warnings.append(f"{self._bigint} целых чисел длиннее 15 цифр записаны как текст (точность Excel)")
        return self._finish()

    def abort(self) -> None:
        try:
            self._wb.close()
        except Exception:  # noqa: BLE001
            pass
        super().abort()


def open_writer(path, fmt: str, columns: Sequence[str], **options):
    fmt = fmt.lower()
    if fmt == "csv":
        return CsvWriter(path, columns, **{k: v for k, v in options.items()
                                           if k in ("delimiter", "formula_guard", "estimated_bytes")})
    if fmt == "json":
        return JsonWriter(path, columns, **options)
    if fmt == "xlsx":
        return XlsxWriter(path, columns, **options)
    raise ExportError(f"Неизвестный формат экспорта: {fmt}")


def export_rows(path, fmt: str, columns: Sequence[str], rows: Sequence[Sequence], *, cancel_token=None,
                progress=None, chunk: int = 5000, **options) -> ExportResult:
    """Экспорт уже загруженных строк (показанных в таблице)."""
    est = max(len(rows), 1) * max(len(columns), 1) * 12
    writer = open_writer(path, fmt, columns, estimated_bytes=est, **options)
    try:
        for i in range(0, len(rows), chunk):
            if cancel_token is not None and cancel_token.is_cancelled:
                raise InterruptedError("cancelled")
            writer.write_rows(rows[i:i + chunk])
            if progress:
                progress(min(1.0, (i + chunk) / max(len(rows), 1)))
        return writer.close()
    except BaseException:
        writer.abort()
        raise


def rows_to_tsv(columns: Sequence[str], rows: Iterable[Sequence], with_header: bool = False) -> str:
    """Текст для буфера обмена (вставка в Excel): табы между ячейками, переводы строк внутри значений — пробелы."""
    def cell(v) -> str:
        return export_text(v).replace("\t", " ").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    lines = []
    if with_header:
        lines.append("\t".join(cell(c) for c in columns))
    lines.extend("\t".join(cell(v) for v in row) for row in rows)
    return "\n".join(lines)
