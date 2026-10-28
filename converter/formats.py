"""Форматы и планировщик маршрутов конвертации.

Для каждой пары «исходный → целевой» маршрут либо существует (прямой на Python, через LibreOffice, или цепочка
из двух шагов), либо нет — тогда программа честно сообщает причину. «Любой в любой» не обещается.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .libreoffice import INSTALL_HINT


@dataclass(frozen=True)
class Fmt:
    id: str
    title: str
    exts: tuple[str, ...]
    kind: str            # document | table | text


FORMATS: dict[str, Fmt] = {f.id: f for f in (
    Fmt("docx", "Word (.docx)", (".docx",), "document"), Fmt("doc", "Word 97–2003 (.doc)", (".doc",), "document"),
    Fmt("odt", "OpenDocument (.odt)", (".odt",), "document"), Fmt("rtf", "RTF (.rtf)", (".rtf",), "document"),
    Fmt("pdf", "PDF (.pdf)", (".pdf",), "document"), Fmt("html", "HTML (.html)", (".html", ".htm"), "document"),
    Fmt("md", "Markdown (.md)", (".md", ".markdown"), "document"), Fmt("txt", "Текст (.txt)", (".txt",), "text"),
    Fmt("sql", "SQL-скрипт (.sql)", (".sql",), "text"),
    Fmt("xlsx", "Excel (.xlsx)", (".xlsx",), "table"), Fmt("xls", "Excel 97–2003 (.xls)", (".xls",), "table"),
    Fmt("csv", "CSV (.csv)", (".csv",), "table"), Fmt("tsv", "TSV (.tsv)", (".tsv",), "table"),
    Fmt("json", "JSON (.json)", (".json",), "table"),
)}
EXT_TO_FMT = {e: f.id for f in FORMATS.values() for e in f.exts}
PRIMARY_EXT = {f.id: f.exts[0] for f in FORMATS.values()}
SOURCE_FORMATS = ["docx", "doc", "odt", "rtf", "html", "md", "txt", "sql", "xlsx", "xls", "csv", "tsv", "json"]
TARGET_FORMATS = ["docx", "doc", "odt", "rtf", "pdf", "html", "md", "txt", "sql", "xlsx", "xls", "csv", "tsv", "json"]

LO_DOC_READ = {"docx", "doc", "odt", "rtf", "html", "txt"}
LO_DOC_WRITE = {"pdf", "docx", "doc", "odt", "rtf", "txt", "html"}
PY_DOC_READ = {"docx", "md", "html", "txt"}
PY_DOC_WRITE = {"md", "html", "txt", "docx"}
PY_TABLE = {"xlsx", "csv", "tsv", "json"}
EXTRACT_SRC = {"docx", "html", "md"}
LO_ONLY_DOC_SRC = {"doc", "odt", "rtf"}

# целевой формат LibreOffice и фильтр импорта исходного
LO_TARGET_SPEC = {"pdf": "pdf", "docx": "docx:MS Word 2007 XML", "doc": "doc:MS Word 97", "odt": "odt",
                  "rtf": "rtf", "txt": "txt:Text (encoded):UTF8", "html": "html:XHTML Writer File:UTF8",
                  "xlsx": "xlsx:Calc MS Excel 2007 XML", "xls": "xls:MS Excel 97"}
LO_INFILTER = {"html": "HTML (StarWriter)", "txt": "Text (encoded):UTF8"}


@dataclass(frozen=True)
class Route:
    src: str
    dst: str
    kind: str                    # python | libreoffice | chain | copy
    via: str = ""                # промежуточный формат цепочки
    needs_lo: bool = False
    notes: tuple[str, ...] = ()

    @property
    def description(self) -> str:
        return {"python": "встроенный преобразователь", "libreoffice": "LibreOffice",
                "chain": f"два шага через {FORMATS[self.via].title if self.via else '…'}",
                "copy": "перекодирование текста"}[self.kind]


@dataclass(frozen=True)
class Capabilities:
    soffice: str | None = None

    @property
    def has_lo(self) -> bool:
        return bool(self.soffice)


def _notes(src: str, dst: str, kind: str) -> tuple[str, ...]:
    n: list[str] = []
    s_doc, d_doc = FORMATS[src].kind != "table", FORMATS[dst].kind != "table"
    if src in ("docx",) and dst in ("md", "txt", "html") or (kind == "chain" and src in LO_ONLY_DOC_SRC and dst == "md"):
        n.append("Переносятся заголовки, абзацы, списки, таблицы, полужирный/курсив и ссылки; изображения, колонтитулы, "
                 "сноски и сложное оформление не переносятся.")
    if src in ("md", "html", "txt") and dst in ("docx", "pdf", "doc", "odt", "rtf") and kind != "libreoffice":
        n.append("Оформление упрощённое (стили Word по умолчанию); CSS и изображения не переносятся.")
    if dst == "txt" and src != "sql":
        n.append("Результат — только текст, без форматирования.")
    if dst == "md" and src in ("html", "txt"):
        n.append("Переносится структура разметки; сложное оформление теряется.")
    if kind == "libreoffice" and dst == "pdf":
        n.append("PDF формирует LibreOffice; вид зависит от шрифтов, установленных в системе.")
    if kind in ("libreoffice", "chain") and dst in ("doc", "xls", "odt", "rtf"):
        n.append("Форматирование сохраняется средствами LibreOffice; возможны небольшие отличия вёрстки.")
    if src == "docx" and dst == "html":
        n.append("Результат — один файл HTML без изображений.")
    if src in PY_TABLE | {"xls"} and dst in PY_TABLE | {"xls"}:
        n.append("Первая строка считается заголовком. Для xlsx сохраняются значения (формулы — вычисленные значения), "
                 "форматы ячеек не переносятся.")
        if src in ("xlsx", "xls") and dst in ("csv", "tsv", "json"):
            n.append("Если в книге несколько листов, для каждого создаётся отдельный файл.")
        if src == "json":
            n.append("Вложенные объекты и массивы записываются как текст JSON.")
    if src in EXTRACT_SRC | LO_ONLY_DOC_SRC and FORMATS[dst].kind == "table":
        n.append("Из документа извлекаются только таблицы (каждая — отдельный лист/файл); текст вне таблиц игнорируется.")
    if dst == "doc" and kind == "chain":
        n.append("DOC формируется через промежуточный ODT: так LibreOffice надёжнее сохраняет таблицы.")
    if src in ("csv", "tsv", "json", "xlsx", "xls") and dst == "pdf":
        n.append("Таблица печатается как есть: широкие таблицы могут занимать несколько страниц.")
    _ = s_doc, d_doc
    return tuple(n)


def plan(src: str, dst: str) -> Route | None:
    """Маршрут преобразования или None, если пара не поддерживается."""
    if src == dst or src not in SOURCE_FORMATS or dst not in TARGET_FORMATS:
        return None
    sk, dk = FORMATS[src].kind, FORMATS[dst].kind
    R = lambda kind, via="", lo=False: Route(src, dst, kind, via, lo, _notes(src, dst, kind))
    # SQL <-> TXT
    if {src, dst} == {"sql", "txt"}:
        return R("copy")
    if "sql" in (src, dst):
        return None
    # документы -> документы
    if sk != "table" and dk != "table":
        # LibreOffice при прямой записи в .doc теряет таблицы у части DOCX; через ODT таблицы сохраняются
        if dst == "doc" and src != "odt" and (src in LO_DOC_READ or src in PY_DOC_READ):
            return R("chain", "odt", True)
        if src in PY_DOC_READ and dst in PY_DOC_WRITE:
            return R("python")
        if src in LO_DOC_READ and dst in LO_DOC_WRITE:
            return R("libreoffice", lo=True)
        if src in PY_DOC_READ and dst in LO_DOC_WRITE:
            return R("chain", "docx", True)                         # md -> docx -> (pdf/doc/odt/rtf)
        if src in LO_ONLY_DOC_SRC and dst in PY_DOC_WRITE:
            return R("chain", "docx", True)                         # doc/odt/rtf -> docx -> md
        return None
    # таблицы -> таблицы
    if sk == "table" and dk == "table":
        if src in PY_TABLE and dst in PY_TABLE:
            return R("python")
        if src == "xls":
            return R("libreoffice", lo=True) if dst == "xlsx" else R("chain", "xlsx", True)
        if dst == "xls":
            return R("libreoffice", lo=True) if src == "xlsx" else R("chain", "xlsx", True)
        return None
    # таблицы -> PDF
    if sk == "table" and dst == "pdf":
        return R("libreoffice", lo=True) if src in ("xlsx", "xls") else R("chain", "xlsx", True)
    # документы -> таблицы (извлечение таблиц)
    if sk != "table" and dk == "table" and dst in PY_TABLE:
        if src in EXTRACT_SRC:
            return R("python")
        if src in LO_ONLY_DOC_SRC:
            return R("chain", "docx", True)
    return None


def explain_unavailable(src: str, dst: str) -> str:
    """Почему пара не поддерживается (для подсказок в интерфейсе)."""
    if src == dst:
        return "Исходный и целевой форматы совпадают."
    if "sql" in (src, dst):
        return "SQL-скрипты преобразуются только в TXT и обратно."
    if FORMATS[src].kind == "table" and FORMATS[dst].kind != "table" and dst != "pdf":
        return "Таблицу нельзя надёжно превратить в текстовый документ; доступны другие табличные форматы и PDF."
    if FORMATS[dst].kind == "table" and src in ("txt", "pdf"):
        return "В этом формате нет таблиц для извлечения."
    if dst == "pdf" and src == "pdf":
        return "Файл уже в PDF."
    return "Для этой пары форматов надёжного преобразования без потери содержимого нет."


def available(route: Route | None, caps: Capabilities) -> tuple[bool, str]:
    if route is None:
        return False, "Не поддерживается"
    if route.needs_lo and not caps.has_lo:
        return False, INSTALL_HINT
    return True, ""


def targets_for(src: str, caps: Capabilities) -> list[tuple[str, Route | None, bool, str]]:
    """Все целевые форматы для источника: (формат, маршрут, доступен, пояснение)."""
    out = []
    for dst in TARGET_FORMATS:
        r = plan(src, dst)
        if r is None:
            if dst != src:
                out.append((dst, None, False, explain_unavailable(src, dst)))
            continue
        ok, why = available(r, caps)
        out.append((dst, r, ok, why))
    return out


def detect_format(path: str | Path) -> str | None:
    return EXT_TO_FMT.get(Path(path).suffix.lower())


_ZIP, _OLE = b"PK\x03\x04", b"\xd0\xcf\x11\xe0"


def resolve_source(path: str | Path) -> tuple[str | None, list[str], str]:
    """(формат, заметки, ошибка). Учитывает «переименованные» файлы (.doc, который на самом деле .docx и т. п.)."""
    p = Path(path)
    fmt = detect_format(p)
    if fmt is None:
        return None, [], f"Формат «{p.suffix or 'без расширения'}» не поддерживается."
    if fmt not in SOURCE_FORMATS:
        return None, [], (f"Формат {FORMATS[fmt].title} не поддерживается как исходный: без потери содержимого его не "
                          "преобразовать (он доступен только как результат).")
    try:
        with open(p, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        return None, [], f"Не удалось прочитать файл: {exc.strerror or exc}"
    if not head:
        return None, [], "Файл пустой."
    notes: list[str] = []
    zip_like, ole_like = head.startswith(_ZIP), head.startswith(_OLE)
    swap = {("doc", True): "docx", ("xls", True): "xlsx", ("docx", False): None, ("xlsx", False): None}
    if fmt in ("doc", "xls") and zip_like:
        fixed = "docx" if fmt == "doc" else "xlsx"
        notes.append(f"Файл .{fmt} на самом деле в формате {fixed.upper()} — обработан как {fixed.upper()}.")
        return fixed, notes, ""
    if fmt in ("docx", "xlsx", "odt") and not zip_like:
        if fmt in ("docx", "xlsx") and ole_like:
            fixed = "doc" if fmt == "docx" else "xls"
            notes.append(f"Файл .{fmt} на самом деле в старом формате {fixed.upper()} — обработан как {fixed.upper()}.")
            return fixed, notes, ""
        return None, [], f"Файл повреждён или не является {fmt.upper()} (нет сигнатуры ZIP)."
    if fmt in ("doc", "xls") and not ole_like:
        return None, [], f"Файл повреждён или не является {fmt.upper()}."
    if fmt == "rtf" and not head.startswith(b"{\\rtf"):
        return None, [], "Файл повреждён или не является RTF."
    _ = swap
    return fmt, notes, ""
