"""Модель шаблона отчёта (хранится в JSON) и его проверка."""
from __future__ import annotations

import dataclasses
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
BLOCK_TYPES = ("heading", "text", "table", "query", "stats", "chart", "summary", "notes", "page_break")
BLOCK_TITLES = {"heading": "Заголовок", "text": "Текст", "table": "Таблица", "query": "Код запроса и результат",
                "stats": "Статистика", "chart": "Диаграмма", "summary": "Выводы", "notes": "Примечания",
                "page_break": "Разрыв страницы"}
DATA_BLOCKS = ("table", "query", "stats", "chart", "summary")      # блоки, которым нужен источник данных
STYLE_IDS = ("business", "academic", "minimal", "analytic", "technical", "financial")
SOURCE_KINDS = ("sql", "table", "procedure")
PARAM_TYPES = ("text", "int", "decimal", "date", "bool")
FORMATS = ("docx", "pdf", "xlsx", "csv", "html", "md")
FORMAT_TITLES = {"docx": "Word (.docx)", "pdf": "PDF (.pdf)", "xlsx": "Excel (.xlsx)", "csv": "CSV (.csv)",
                 "html": "HTML (.html)", "md": "Markdown (.md)"}
COL_FORMATS = ("auto", "text", "integer", "decimal", "currency", "percent", "date", "datetime")
TOTALS = ("none", "sum", "avg", "count")
FILTER_OPS = ("=", "<>", "<", ">", "<=", ">=", "like", "in", "is_null", "not_null")
AGG_FUNCS = ("sum", "avg", "min", "max", "count", "count_distinct")
METRIC_OPS = ("sum", "avg", "min", "max", "count", "distinct", "ratio")
CHART_KINDS = ("bar", "line", "pie")
CHART_AGGS = ("sum", "avg", "count", "min", "max")
COMPUTED_OPS = ("add", "sub", "mul", "div", "pct")

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def new_id(prefix: str = "b") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _pick(cls, data: dict) -> dict:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in names}


@dataclass
class Param:
    name: str = ""
    label: str = ""
    type: str = "text"
    default: str = ""
    required: bool = False


@dataclass
class Filter:
    column: str = ""
    op: str = "="
    value: str = ""          # литерал или «:имя_параметра»; для in — значения через «;»


@dataclass
class Aggregate:
    column: str = "*"
    func: str = "sum"
    alias: str = ""


@dataclass
class SortSpec:
    column: str = ""
    desc: bool = False


@dataclass
class DataSource:
    id: str = ""
    name: str = ""
    kind: str = "sql"
    database: str = ""
    sql: str = ""                                   # kind=sql: SELECT-запрос с параметрами :имя
    schema: str = "dbo"                             # kind=table
    table: str = ""
    columns: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    order_by: list[SortSpec] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    aggregates: list[Aggregate] = field(default_factory=list)
    row_limit: int = 0                              # 0 — по общему лимиту отчёта
    procedure: str = ""                             # kind=procedure: схема.имя
    proc_params: dict[str, str] = field(default_factory=dict)
    proc_trusted: bool = False                      # пользователь подтвердил, что процедура только читает

    @classmethod
    def from_dict(cls, d: dict) -> "DataSource":
        o = cls(**_pick(cls, d))
        o.filters = [Filter(**_pick(Filter, x)) for x in d.get("filters", [])]
        o.order_by = [SortSpec(**_pick(SortSpec, x)) for x in d.get("order_by", [])]
        o.aggregates = [Aggregate(**_pick(Aggregate, x)) for x in d.get("aggregates", [])]
        return o


@dataclass
class ColumnSpec:
    name: str = ""
    title: str = ""
    hidden: bool = False
    format: str = "auto"
    decimals: int = -1                              # -1 — автоматически
    total: str = "none"
    align: str = "auto"                             # auto | left | right | center


@dataclass
class ComputedColumn:
    title: str = ""
    op: str = "div"
    a: str = ""                                     # имя столбца или число
    b: str = ""
    decimals: int = 2


@dataclass
class Metric:
    title: str = ""
    op: str = "sum"
    column: str = ""
    column2: str = ""                               # для ratio: sum(column) / sum(column2)


@dataclass
class Block:
    id: str = ""
    type: str = "text"
    title: str = ""
    text: str = ""
    level: int = 1
    source: str = ""
    columns: list[ColumnSpec] = field(default_factory=list)
    sort: list[SortSpec] = field(default_factory=list)
    max_rows: int = 0                               # 0 — общий лимит
    group_by: str = ""
    computed: list[ComputedColumn] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    row_numbers: bool = False
    show_code: bool = True                          # блок «Код запроса и результат»: показывать текст запроса
    show_result: bool = True                        # … и результат его выполнения
    chart_kind: str = "bar"
    x: str = ""
    y: list[str] = field(default_factory=list)
    agg: str = "sum"
    top_n: int = 10
    stat_columns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        o = cls(**_pick(cls, d))
        o.columns = [ColumnSpec(**_pick(ColumnSpec, x)) for x in d.get("columns", [])]
        o.sort = [SortSpec(**_pick(SortSpec, x)) for x in d.get("sort", [])]
        o.computed = [ComputedColumn(**_pick(ComputedColumn, x)) for x in d.get("computed", [])]
        o.metrics = [Metric(**_pick(Metric, x)) for x in d.get("metrics", [])]
        return o


@dataclass
class ReportTemplate:
    schema_version: int = SCHEMA_VERSION
    id: str = ""
    name: str = "Новый отчёт"
    description: str = ""
    style: str = "business"
    font_name: str = ""                             # пусто — шрифт стиля
    font_size: float = 0                            # 0 — размер стиля
    accent: str = ""                                # #RRGGBB, пусто — цвет стиля
    orientation: str = "portrait"
    margin_cm: float = 2.0
    title: str = "{{report_name}}"
    subtitle: str = ""
    organization: str = ""
    author: str = ""
    locale: str = "ru"
    currency_symbol: str = ""
    title_page: bool = True
    toc: bool = True
    header_text: str = "{{report_title}}"
    footer_text: str = ""
    page_numbers: bool = True
    include_sql: bool = False
    base_docx: str = ""                             # пользовательский Word-шаблон (.docx/.dotx)
    continue_on_error: bool = False
    part_rows: int = 0                              # >0: большие таблицы делятся на части (файлы)
    params: list[Param] = field(default_factory=list)
    sources: list[DataSource] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReportTemplate":
        o = cls(**_pick(cls, d))
        o.params = [Param(**_pick(Param, x)) for x in d.get("params", [])]
        o.sources = [DataSource.from_dict(x) for x in d.get("sources", [])]
        o.blocks = [Block.from_dict(x) for x in d.get("blocks", [])]
        if not o.id:
            o.id = new_id("t")
        return o

    def source(self, source_id: str) -> DataSource | None:
        return next((s for s in self.sources if s.id == source_id), None)


def validate_template(t: ReportTemplate) -> list[str]:
    """Список понятных ошибок; пустой — шаблон корректен."""
    from database.sql_guard import analyze
    from .variables import find_named_params

    errs: list[str] = []
    if not t.name.strip():
        errs.append("Укажите название шаблона.")
    if t.style not in STYLE_IDS:
        errs.append(f"Неизвестный стиль оформления: {t.style}.")
    if t.orientation not in ("portrait", "landscape"):
        errs.append("Ориентация страницы: portrait или landscape.")
    if t.locale not in ("ru", "en"):
        errs.append("Язык форматирования: ru или en.")
    if t.accent and not re.fullmatch(r"#[0-9A-Fa-f]{6}", t.accent):
        errs.append("Цвет акцента должен быть в формате #RRGGBB.")
    if not (0.5 <= t.margin_cm <= 6):
        errs.append("Поля страницы: от 0,5 до 6 см.")
    if t.font_size and not (6 <= t.font_size <= 24):
        errs.append("Размер шрифта: от 6 до 24 пт.")
    if t.part_rows < 0:
        errs.append("Размер части отчёта не может быть отрицательным.")

    names = set()
    for p in t.params:
        if not _NAME_RE.match(p.name):
            errs.append(f"Параметр «{p.name}»: имя — латиница, цифры и _, не с цифры.")
        if p.name in names:
            errs.append(f"Параметр «{p.name}» повторяется.")
        names.add(p.name)
        if p.type not in PARAM_TYPES:
            errs.append(f"Параметр «{p.name}»: неизвестный тип {p.type}.")

    ids = set()
    for s in t.sources:
        label = f"Источник «{s.name or s.id}»"
        if not _ID_RE.match(s.id):
            errs.append(f"{label}: недопустимый идентификатор (латиница, цифры, - и _).")
        if s.id in ids:
            errs.append(f"{label}: идентификатор повторяется.")
        ids.add(s.id)
        if s.kind not in SOURCE_KINDS:
            errs.append(f"{label}: неизвестный тип источника.")
        elif s.kind == "sql":
            if not s.sql.strip():
                errs.append(f"{label}: пустой SQL-запрос.")
            else:
                a = analyze(re.sub(r"(?<![:\w]):[A-Za-z_]\w*", "@p", s.sql))
                if a.errors or len(a.batches) != 1:
                    errs.append(f"{label}: допускается ровно один пакет без GO.")
                elif not a.is_read_only:
                    errs.append(f"{label}: запрос изменяет данные или схему — в отчётах разрешено только чтение "
                                f"({'; '.join(a.reasons[:2])}).")
                for n in find_named_params(s.sql):
                    if n not in names:
                        errs.append(f"{label}: параметр :{n} не объявлен в шаблоне.")
        elif s.kind == "table":
            if not s.table.strip():
                errs.append(f"{label}: не указана таблица или представление.")
            for f in s.filters:
                if f.op not in FILTER_OPS:
                    errs.append(f"{label}: неизвестный оператор фильтра {f.op}.")
                if f.value.startswith(":") and f.value[1:] not in names:
                    errs.append(f"{label}: параметр {f.value} не объявлен.")
            for a in s.aggregates:
                if a.func not in AGG_FUNCS:
                    errs.append(f"{label}: неизвестная агрегатная функция {a.func}.")
        elif s.kind == "procedure":
            if not s.procedure.strip():
                errs.append(f"{label}: не указана процедура.")
            elif not s.proc_trusted:
                errs.append(f"{label}: подтвердите, что процедура только читает данные.")
            for k, v in s.proc_params.items():
                if not _NAME_RE.match(k):
                    errs.append(f"{label}: недопустимое имя параметра процедуры «{k}».")
                if v.startswith(":") and v[1:] not in names:
                    errs.append(f"{label}: параметр {v} не объявлен.")

    block_ids = set()
    for b in t.blocks:
        label = f"Блок «{b.title or BLOCK_TITLES.get(b.type, b.type)}»"
        if b.type not in BLOCK_TYPES:
            errs.append(f"{label}: неизвестный тип блока {b.type}.")
            continue
        if not _ID_RE.match(b.id) or b.id in block_ids:
            errs.append(f"{label}: идентификатор блока пуст или повторяется.")
        block_ids.add(b.id)
        if b.type in DATA_BLOCKS and b.source not in ids:
            errs.append(f"{label}: не выбран существующий источник данных.")
        if b.type == "heading" and not b.title.strip():
            errs.append(f"{label}: пустой заголовок.")
        if b.type == "heading" and not (1 <= b.level <= 3):
            errs.append(f"{label}: уровень заголовка 1–3.")
        if b.type == "chart":
            if b.chart_kind not in CHART_KINDS:
                errs.append(f"{label}: неизвестный вид диаграммы.")
            if not b.x:
                errs.append(f"{label}: не выбран столбец для оси X (категории).")
            if b.agg not in CHART_AGGS:
                errs.append(f"{label}: неизвестная агрегация {b.agg}.")
            if b.agg != "count" and not b.y:
                errs.append(f"{label}: не выбран числовой столбец (Y).")
            if not (2 <= b.top_n <= 50):
                errs.append(f"{label}: «Top N» — от 2 до 50.")
        if b.type == "table":
            for c in b.columns:
                if c.format not in COL_FORMATS or c.total not in TOTALS:
                    errs.append(f"{label}: столбец «{c.name}» — недопустимый формат или итог.")
            for c in b.computed:
                if c.op not in COMPUTED_OPS:
                    errs.append(f"{label}: вычисляемый столбец «{c.title}» — неизвестная операция.")
        if b.type == "stats":
            for m in b.metrics:
                if m.op not in METRIC_OPS or (m.op == "ratio" and not m.column2):
                    errs.append(f"{label}: показатель «{m.title}» задан некорректно.")
    return errs
