"""Промежуточное представление отчёта и его построение из шаблона и данных.

Одно представление (ReportDoc) рендерится в DOCX, HTML, Markdown, XLSX и CSV — так содержимое
всех форматов совпадает, а особенности формата учитываются только в рендерере.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .charts import ChartNotApplicable, chart_data, render_chart
from .data import Dataset
from .formatting import Formatter, clean_xml_text, looks_like_identifier
from .model import DATA_BLOCKS, Block, ColumnSpec, ReportTemplate
from .query_builder import build_query
from .stats import (BINARY, DATE, DATETIME, INTEGER, NUMERIC, TEXT, aggregate_by, column_stats, conclusions,
                    dataset_stats, exact_sum, infer_kind, metric_value, time_series, _is_num)
from .styles import StylePreset, get_style
from .variables import find_named_params, render_text
from utils.logging_setup import redact
from utils.formatting import sort_key

MAX_GROUP_SECTIONS = 100


@dataclass
class Cell:
    text: str
    raw: Any = None
    align: str = "left"
    empty: bool = False
    bold: bool = False


@dataclass
class DocColumn:
    title: str
    align: str = "left"
    width: int = 10
    kind: str = "text"
    fmt: str = "auto"


@dataclass
class DocTable:
    title: str
    columns: list[DocColumn]
    rows: list[list[Cell]]
    totals: list[Cell] | None = None
    note: str = ""
    source_id: str = ""
    compact: bool = False
    show_title: bool = True        # False, если заголовок уже выведен отдельным DocHeading


@dataclass
class DocHeading:
    level: int
    text: str


@dataclass
class DocParagraph:
    text: str
    style: str = "normal"     # normal | note | caption | code


@dataclass
class DocBullets:
    items: list[str]


@dataclass
class DocChart:
    title: str
    png: bytes
    alt: str = ""


@dataclass
class DocKeyValues:
    title: str
    items: list[tuple[str, str]]


@dataclass
class DocPageBreak:
    pass


@dataclass
class DocToc:
    pass


@dataclass
class DocTitlePage:
    title: str
    subtitle: str = ""
    organization: str = ""
    author: str = ""
    generated: str = ""
    description: str = ""
    part_label: str = ""


@dataclass
class ReportDoc:
    template: ReportTemplate
    style: StylePreset
    title: str
    header: str
    footer: str
    author: str
    organization: str
    generated_at: dt.datetime
    elements: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    part: tuple[int, int] | None = None     # (номер части с 0, всего)
    formatter: Formatter | None = None
    variables: dict = field(default_factory=dict)

    def headings(self, max_level: int = 3) -> list[DocHeading]:
        return [e for e in self.elements if isinstance(e, DocHeading) and e.level <= max_level]

    def tables(self) -> list[DocTable]:
        return [e for e in self.elements if isinstance(e, DocTable)]


@dataclass
class BuildContext:
    template: ReportTemplate
    datasets: dict[str, Dataset]
    param_values: dict[str, Any] = field(default_factory=dict)
    raw_params: dict[str, str] = field(default_factory=dict)
    generated_at: dt.datetime = field(default_factory=dt.datetime.now)
    server: str = ""
    database: str = ""
    max_rows: int = 5000
    part: tuple[int, int] | None = None
    app_version: str = ""


# ---------------------------------------------------------------- переменные
def _variables(ctx: BuildContext, fmt: Formatter, title: str | None) -> dict[str, str]:
    t = ctx.template
    v = {"report_name": t.name, "organization": t.organization, "author": t.author,
         "date": fmt.date(ctx.generated_at), "datetime": fmt.datetime(ctx.generated_at),
         "database": ctx.database, "server": ctx.server}
    if title is not None:
        v["report_title"] = title
    for p in t.params:
        val = ctx.param_values.get(p.name)
        if val is None:
            v[f"param.{p.name}"] = ""
        elif isinstance(val, (str, int)) and not isinstance(val, bool):
            v[f"param.{p.name}"] = str(val)
        else:
            v[f"param.{p.name}"] = fmt.value(val)
    from .model import Metric
    for sid, ds in ctx.datasets.items():
        v[f"rows.{sid}"] = fmt.number(len(ds.rows), 0)
        for i, c in enumerate(ds.columns):
            vals = [r[i] for r in ds.rows]
            if infer_kind(vals) in (NUMERIC, INTEGER):
                for op in ("sum", "avg", "min", "max", "count", "distinct"):
                    res = metric_value(ds.columns, ds.rows, Metric(op=op, column=c))
                    if res is not None:
                        v[f"stat.{sid}.{c}.{op}"] = fmt.number(res, 0 if op in ("count", "distinct") else None) \
                            if op in ("count", "distinct") else fmt.number(res, 2)
    return v


# ---------------------------------------------------------------- таблицы
def _sort_rows(rows: list[tuple], specs, idx: dict[str, int]) -> list[tuple]:
    for s in reversed(specs):
        if s.column not in idx:
            continue
        i = idx[s.column]
        nn = [r for r in rows if r[i] is not None]
        nulls = [r for r in rows if r[i] is None]
        nn.sort(key=lambda r: sort_key(r[i]), reverse=s.desc)
        rows = nn + nulls
    return rows


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float, Decimal)):
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return Decimal(str(x)) if not isinstance(x, Decimal) else x
    return None


def _operand(token: str, row: tuple, idx: dict[str, int]):
    if token in idx:
        return _num(row[idx[token]])
    try:
        return Decimal(token.replace(",", "."))
    except Exception:  # noqa: BLE001
        return None


def _compute(op: str, a, b):
    if a is None or b is None:
        return None
    if op == "add":
        return a + b
    if op == "sub":
        return a - b
    if op == "mul":
        return a * b
    if b == 0:
        return None
    return a / b if op == "div" else a / b * 100


def _width(title: str, texts: list[str]) -> int:
    lens = sorted(len(t) for t in texts) or [0]
    p90 = lens[min(len(lens) - 1, int(len(lens) * 0.9))]
    return max(4, min(40, max(len(title), p90)))


def _table_columns(block: Block, ds: Dataset, ctx: BuildContext, warnings: list[str]) -> list[ColumnSpec]:
    names = ds.columns
    if block.columns:
        out = []
        for c in block.columns:
            if c.name not in names:
                warnings.append(f"Таблица «{block.title or ds.name}»: столбца «{c.name}» нет в источнике.")
            elif not c.hidden:
                out.append(c)
        return out
    out = []
    for i, n in enumerate(names):
        if infer_kind([r[i] for r in ds.rows[:500]]) == BINARY:
            continue                                  # служебные бинарные поля (rowversion и т. п.)
        out.append(ColumnSpec(name=n))
    return out


def _build_tables(block: Block, ds: Dataset, ctx: BuildContext, fmt: Formatter, style: StylePreset,
                  warnings: list[str], limit: int, level: int) -> list:
    idx = {c: i for i, c in enumerate(ds.columns)}
    specs = _table_columns(block, ds, ctx, warnings)
    rows = _sort_rows(list(ds.rows), block.sort, idx)
    if block.sort and ds.truncated:
        warnings.append(f"Таблица «{block.title or ds.name}»: сортировка применена только к полученным строкам.")
    total_rows = len(rows)
    part_note = ""
    if block.group_by and block.group_by in idx:
        return _group_sections(block, ds, rows, idx, specs, ctx, fmt, style, warnings, limit, level)

    shown = rows[:limit]
    if ctx.part is not None and ctx.template.part_rows > 0 and len(shown) > ctx.template.part_rows:
        pr = ctx.template.part_rows
        a, b = ctx.part[0] * pr, min(len(shown), (ctx.part[0] + 1) * pr)
        part_note = f"Строки {a + 1}–{b} из {len(shown)} (часть {ctx.part[0] + 1} из {ctx.part[1]}). "
        shown = shown[a:b]
    table = _make_table(block.title or ds.name, specs, block, shown, idx, ctx, fmt, ds)
    notes = []
    if part_note:
        notes.append(part_note.strip())
    if total_rows > limit:
        notes.append(f"Показано {fmt.number(limit, 0)} из {fmt.number(total_rows, 0)} полученных строк"
                     + (" (в источнике их больше)." if ds.truncated else "."))
        warnings.append(f"Таблица «{block.title or ds.name}»: показана часть строк ({limit} из {total_rows}).")
    elif ds.truncated:
        notes.append(f"Показаны первые {fmt.number(total_rows, 0)} строк; в источнике их больше.")
    if table.totals and (total_rows > limit or part_note):
        notes.append("Итоги рассчитаны по показанным строкам.")
    table.note = " ".join(notes)
    if not shown and not part_note:
        table.note = (table.note + " Нет данных.").strip()
    return [table]


def _make_table(title: str, specs: list[ColumnSpec], block: Block, rows: list[tuple], idx: dict[str, int],
                ctx: BuildContext, fmt: Formatter, ds: Dataset) -> DocTable:
    cols: list[DocColumn] = []
    cells_cols: list[list[Cell]] = []
    totals: list[Cell] = []
    has_totals = False

    def add_col(col: DocColumn, cells: list[Cell], total: Cell):
        cols.append(col)
        cells_cols.append(cells)
        totals.append(total)

    if block.row_numbers:
        add_col(DocColumn("№", "right", 4, "integer"),
                [Cell(str(i + 1), i + 1, "right") for i in range(len(rows))], Cell(""))
    for spec in specs:
        i = idx[spec.name]
        vals = [r[i] for r in rows]
        kind = infer_kind(vals)
        grouping = not looks_like_identifier(spec.name)
        align = spec.align if spec.align != "auto" else ("right" if kind in (NUMERIC, INTEGER) or spec.format in
                                                         ("integer", "decimal", "currency", "percent") else "left")
        all_midnight = kind == DATE
        cells = []
        for v in vals:
            text = fmt.value(v, spec.format, spec.decimals, grouping=grouping, dates_only=all_midnight)
            cells.append(Cell(text, v, align, v is None))
        total = Cell("")
        if spec.total != "none":
            nums = [x for x in (_num(v) for v in vals) if x is not None]
            if spec.total == "count":
                n = sum(1 for v in vals if v is not None)
                total = Cell(fmt.number(n, 0), n, "right", bold=True)
                has_totals = True
            elif nums:
                s = exact_sum(nums)
                res = s if spec.total == "sum" else s / (Decimal(len(nums)) if isinstance(s, Decimal) else len(nums))
                text = fmt.value(res, spec.format if spec.format != "auto" else "decimal" if kind == NUMERIC else "auto",
                                 spec.decimals if spec.decimals >= 0 else (2 if spec.total == "avg" else -1))
                total = Cell(text, res, "right", bold=True)
                has_totals = True
        add_col(DocColumn(clean_xml_text(spec.title or spec.name), align, _width(spec.title or spec.name, [c.text for c in cells]),
                          kind, spec.format), cells, total)
    for cc in block.computed:
        cells = []
        for r in rows:
            val = _compute(cc.op, _operand(cc.a, r, idx), _operand(cc.b, r, idx))
            if val is not None:
                val = val.quantize(Decimal(1).scaleb(-max(0, cc.decimals)))
            text = "" if val is None else fmt.number(val, max(0, cc.decimals))
            if val is not None and cc.op == "pct":
                text += "\u00a0%" if fmt.ru else "%"
            cells.append(Cell(text, val, "right", val is None))
        add_col(DocColumn(cc.title or "Вычисляемый", "right", _width(cc.title, [c.text for c in cells]), "numeric"),
                cells, Cell(""))
    out_rows = [[cells_cols[c][r] for c in range(len(cols))] for r in range(len(rows))]
    final_totals = None
    if has_totals:
        final_totals = totals
        label_col = next((i for i, c in enumerate(totals) if not c.text), None)
        if label_col is not None:
            totals[label_col] = Cell("Итого" if fmt.ru else "Total", None, "left", bold=True)
    return DocTable(clean_xml_text(title), cols, out_rows, final_totals, source_id=ds.source_id)


def _group_sections(block, ds, rows, idx, specs, ctx, fmt, style, warnings, limit, level) -> list:
    gi = idx[block.group_by]
    groups: dict[Any, list[tuple]] = {}
    for r in rows:
        groups.setdefault(r[gi], []).append(r)
    keys = sorted(groups, key=sort_key)
    if len(keys) > MAX_GROUP_SECTIONS:
        warnings.append(f"Таблица «{block.title or ds.name}»: групп больше {MAX_GROUP_SECTIONS}, показаны первые.")
        keys = keys[:MAX_GROUP_SECTIONS]
    sub_specs = [s for s in specs if s.name != block.group_by]
    out: list = []
    remaining = limit
    shown_groups = 0
    for k in keys:
        if remaining <= 0:
            break
        part = groups[k][:remaining]
        remaining -= len(part)
        shown_groups += 1
        label = fmt.value(k) or ("(пусто)" if fmt.ru else "(empty)")
        out.append(DocHeading(min(3, level + 1), f"{block.group_by}: {label}"))
        t = _make_table("", sub_specs, block, part, idx, ctx, fmt, ds)
        if len(part) < len(groups[k]):
            t.note = f"Показано {len(part)} из {len(groups[k])} строк группы."
        out.append(t)
    omitted = len(keys) - shown_groups
    if omitted > 0:
        msg = (f"Достигнут лимит строк ({fmt.number(limit, 0)}): не показано групп — {omitted} из {len(keys)}. "
               "Увеличьте лимит или отфильтруйте данные в источнике.")
        out.append(DocParagraph(msg, "note"))
        warnings.append(f"Таблица «{block.title or ds.name}»: {msg}")
    return out


# ---------------------------------------------------------------- статистика
def _stat_tables(block: Block, ds: Dataset, fmt: Formatter, title: str) -> list:
    out: list = []
    stats = dataset_stats(ds.columns, ds.rows, block.stat_columns or None)
    ru = fmt.ru
    L = (lambda a, b: a if ru else b)
    n = fmt.number

    def table(name, headers, rows, note=""):
        cols = [DocColumn(h, "left" if i == 0 else "right", max(14 if i == 0 else 10, len(h) + 2))
                for i, h in enumerate(headers)]
        out.append(DocTable(name, cols, [[Cell(str(c), None, cols[i].align, c in ("", "—")) for i, c in enumerate(r)]
                                         for r in rows], note=note, compact=True, source_id=ds.source_id))

    out.append(DocParagraph(L(f"Всего записей: {n(len(ds.rows), 0)}.", f"Total records: {n(len(ds.rows), 0)}.")))
    numeric = [s for s in stats if s.kind in (NUMERIC, INTEGER) and s.count]
    if numeric:
        skipped = [s.name for s in numeric if s.skipped_reason]
        table(L("Числовые поля", "Numeric columns"),
              [L("Поле", "Column"), L("Записей", "Count"), L("Пусто", "Empty"), L("Сумма", "Sum"), L("Среднее", "Mean"),
               L("Мин.", "Min"), L("Макс.", "Max"), L("Уникальных", "Distinct")],
              [[s.name, n(s.count, 0), n(s.nulls, 0), "—" if s.total is None else n(s.total, 2 if not isinstance(s.total, int) else 0),
                "—" if s.mean is None else n(s.mean, 2), n(s.minimum), n(s.maximum), n(s.distinct, 0)] for s in numeric],
              L("Для полей-идентификаторов и годов сумма и среднее не рассчитываются: " + ", ".join(skipped) + "."
                if skipped else "", ""))
    dates = [s for s in stats if s.kind in (DATE, DATETIME) and s.count]
    if dates:
        table(L("Поля с датами", "Date columns"),
              [L("Поле", "Column"), L("Записей", "Count"), L("Пусто", "Empty"), L("Раньше всех", "Earliest"),
               L("Позже всех", "Latest"), L("Уникальных", "Distinct")],
              [[s.name, n(s.count, 0), n(s.nulls, 0), fmt.value(s.minimum), fmt.value(s.maximum), n(s.distinct, 0)]
               for s in dates])
    texts = [s for s in stats if s.kind in (TEXT, "bool") and s.count]
    if texts:
        table(L("Текстовые и логические поля", "Text and boolean columns"),
              [L("Поле", "Column"), L("Записей", "Count"), L("Пусто", "Empty"), L("Уникальных", "Distinct"),
               L("Частое значение", "Most common")],
              [[s.name, n(s.count, 0), n(s.nulls, 0), n(s.distinct, 0),
                f"{fmt.value(s.top[0][0])} ({n(s.top[0][1], 0)})" if s.top else "—"] for s in texts])
    if block.metrics:
        rows = []
        for m in block.metrics:
            val = metric_value(ds.columns, ds.rows, m)
            if val is None:
                text = "—"
            elif m.op == "ratio":
                text = n(val, 4)
            elif m.op in ("count", "distinct"):
                text = n(val, 0)
            else:
                text = n(val, 2)
            rows.append([m.title or f"{m.op}({m.column})", text])
        table(L("Показатели", "Metrics"), [L("Показатель", "Metric"), L("Значение", "Value")], rows)

    # динамика по времени и группировка по категориям — только если данные подходят
    date_col = next((s.name for s in dates), None)
    num_cols = [s.name for s in numeric if not s.skipped_reason][:2]
    idx = {c: i for i, c in enumerate(ds.columns)}
    if date_col and num_cols:
        for nc in num_cols:
            pts = time_series(ds.rows, idx[date_col], idx[nc], "sum")
            pts = [(a, b) for a, b in pts if b is not None][-24:]
            if len(pts) >= 2:
                table(L(f"Динамика: {nc} по периодам ({date_col})", f"Trend: {nc} by period ({date_col})"),
                      [L("Период", "Period"), L(f"Сумма: {nc}", f"Sum: {nc}")], [[a, n(b, 2)] for a, b in pts])
    cats = [s for s in texts if 2 <= s.distinct <= 30][:2]
    for s in cats:
        vc = num_cols[0] if num_cols else None
        pairs = aggregate_by(ds.rows, idx[s.name], idx[vc] if vc else None, "sum" if vc else "count")
        pairs = [(k, v) for k, v in pairs if v is not None]
        pairs.sort(key=lambda p: sort_key(p[1]), reverse=True)
        counts = dict(aggregate_by(ds.rows, idx[s.name], None, "count"))
        headers = [s.name, L("Записей", "Count")] + ([L(f"Сумма: {vc}", f"Sum: {vc}")] if vc else [])
        table(L(f"По категориям: {s.name}", f"By category: {s.name}"), headers,
              [[fmt.value(k), n(counts.get(k, 0), 0)] + ([n(v, 2)] if vc else []) for k, v in pairs[:10]],
              L("Показаны 10 крупнейших категорий." if len(pairs) > 10 else "", ""))
    return out


# ---------------------------------------------------------------- сборка
def build_document(ctx: BuildContext) -> ReportDoc:
    t = ctx.template
    style = get_style(t.style)
    fmt = Formatter(t.locale, t.currency_symbol)
    warnings: list[str] = []
    missing: list[str] = []

    def rt(text: str, v: dict[str, str]) -> str:
        out, m = render_text(text, v)
        for x in m:
            if x not in missing:
                missing.append(x)
        return out

    base_vars = _variables(ctx, fmt, None)
    title = rt(t.title, base_vars)
    variables = _variables(ctx, fmt, title)
    doc = ReportDoc(t, style, title, rt(t.header_text, variables), rt(t.footer_text, variables),
                    rt(t.author, variables), rt(t.organization, variables), ctx.generated_at, [], warnings,
                    ctx.part, fmt, variables)
    part_idx = ctx.part[0] if ctx.part else 0
    multipart = bool(ctx.part and ctx.part[1] > 1)
    el = doc.elements

    if t.title_page:
        el.append(DocTitlePage(title, rt(t.subtitle, variables), doc.organization, doc.author,
                               fmt.datetime(ctx.generated_at), "",
                               f"Часть {part_idx + 1} из {ctx.part[1]}" if multipart else ""))
        el.append(DocPageBreak())
    toc_pos = len(el)
    last_level = 1
    for b in t.blocks:
        ds = ctx.datasets.get(b.source) if b.source else None
        limit = b.max_rows or ctx.max_rows
        if part_idx > 0:                               # в частях 2+ — только крупные таблицы
            if b.type != "table" or ds is None or t.part_rows <= 0 or b.group_by or \
                    len(ds.rows[:limit]) <= t.part_rows:
                continue
        if b.type == "heading":
            last_level = b.level
            el.append(DocHeading(b.level, rt(b.title, variables)))
        elif b.type == "text":
            for para in rt(b.text, variables).split("\n\n"):
                if para.strip():
                    el.append(DocParagraph(para.strip()))
        elif b.type == "page_break":
            el.append(DocPageBreak())
        elif b.type == "notes":
            el.extend(_notes(ctx, doc, fmt, b, warnings))
        elif b.type in DATA_BLOCKS:
            if ds is None:
                msg = f"Нет данных источника «{b.source}»."
                warnings.append(f"Блок «{b.title or b.type}»: {msg}")
                el.append(DocParagraph(msg, "note"))
                continue
            heading = rt(b.title, variables) if b.title else None
            lvl = b.level
            if b.type == "table":
                if heading:
                    el.append(DocHeading(lvl, heading))
                built = _build_tables(b, ds, ctx, fmt, style, warnings, limit, lvl)
                for tb in built:
                    if isinstance(tb, DocTable) and heading:
                        tb.show_title = False
                el.extend(built)
            elif b.type == "query":
                el.extend(_query_block(b, ds, ctx, fmt, style, warnings, limit, heading, lvl))
            elif b.type == "stats":
                el.append(DocHeading(lvl, heading or f"Статистика: {ds.name}"))
                el.extend(_stat_tables(b, ds, fmt, heading or ds.name))
            elif b.type == "chart":
                _chart(b, ds, el, warnings, style, fmt, rt(b.title, variables) if b.title else "")
            else:
                el.append(DocHeading(lvl, heading or "Выводы"))
                el.append(DocBullets(conclusions(ds.name, ds.columns, ds.rows, ds.truncated, fmt)))
    if t.toc and any(isinstance(e, DocHeading) and e.level <= 3 for e in el[toc_pos:]):
        el.insert(toc_pos, DocToc())
        if t.title_page:
            pass
    for m in missing:
        warnings.append(f"Неизвестная переменная шаблона: {{{{{m}}}}} (оставлена как есть).")
    _sanitize(doc)
    return doc


def _query_block(b: Block, ds: Dataset, ctx: BuildContext, fmt: Formatter, style: StylePreset, warnings: list[str],
                 limit: int, heading: str | None, level: int) -> list:
    """Код запроса (с параметрами) и результат его выполнения."""
    out: list = [DocHeading(level, heading or f"Запрос «{ds.name}»")]
    src = ctx.template.source(b.source)
    if b.show_code:
        if src is not None and src.kind == "sql" and src.sql.strip():
            code = src.sql.strip()
            names = find_named_params(code)
            values = [(f":{n}", ctx.param_values.get(n)) for n in names]
        else:                                        # таблица / процедура — сформированный программой запрос
            code = (ds.sql or "").strip()
            values = []
            if src is not None:
                try:
                    _sql, vals = build_query(src, ctx.param_values, limit)
                    values = [(f"? № {i + 1}", v) for i, v in enumerate(vals)]
                except Exception:  # noqa: BLE001
                    values = []
        out.append(DocParagraph("Текст запроса (пароли и токены скрыты):", "note"))
        out.append(DocParagraph(redact(code) or "—", "code"))
        if values:
            show = lambda v: "NULL" if v is None else str(v) if isinstance(v, int) and not isinstance(v, bool) else fmt.value(v)
            out.append(DocKeyValues("Значения параметров", [(k, show(v)) for k, v in values]))
    if b.show_result:
        n = len(ds.rows)
        info = f"Результат: строк — {fmt.number(n, 0)}, время выполнения — {ds.elapsed:.2f} с."
        if ds.truncated:
            info += " Получена только часть строк (сработал лимит строк источника)."
        if n > limit:
            info += f" В документе показаны первые {fmt.number(limit, 0)}."
        out.append(DocParagraph(info, "note"))
        if not ds.columns:
            out.append(DocParagraph("Запрос не вернул набор строк.", "note"))
        else:
            for tb in _build_tables(b, ds, ctx, fmt, style, warnings, limit, level):
                if isinstance(tb, DocTable):
                    tb.show_title = False
                out.append(tb)
    return out


def _sanitize(doc: ReportDoc) -> None:
    """Удаляет из всего текста документа символы, недопустимые в XML (ячейки данных уже очищены форматтером)."""
    c = clean_xml_text
    doc.title, doc.header, doc.footer = c(doc.title), c(doc.header), c(doc.footer)
    doc.author, doc.organization = c(doc.author), c(doc.organization)
    doc.warnings[:] = [c(w) for w in doc.warnings]
    for e in doc.elements:
        if isinstance(e, DocHeading):
            e.text = c(e.text)
        elif isinstance(e, DocParagraph):
            e.text = c(e.text)
        elif isinstance(e, DocBullets):
            e.items = [c(i) for i in e.items]
        elif isinstance(e, DocKeyValues):
            e.title = c(e.title)
            e.items = [(c(k), c(v)) for k, v in e.items]
        elif isinstance(e, DocTitlePage):
            e.title, e.subtitle, e.organization = c(e.title), c(e.subtitle), c(e.organization)
            e.author, e.description, e.part_label = c(e.author), c(e.description), c(e.part_label)
        elif isinstance(e, DocChart):
            e.title, e.alt = c(e.title), c(e.alt)
        elif isinstance(e, DocTable):
            e.title, e.note = c(e.title), c(e.note)
            for col in e.columns:
                col.title = c(col.title)


def _chart(b: Block, ds: Dataset, el: list, warnings: list[str], style: StylePreset, fmt: Formatter, title: str) -> None:
    label = title or f"{ds.name}: {b.x}"
    try:
        data = chart_data(b.chart_kind, ds.columns, ds.rows, b.x, b.y, b.agg, b.top_n, fmt)
        png = render_chart(data, b.chart_kind, "" if title else label, style, fmt)
    except ChartNotApplicable as exc:
        msg = f"Диаграмма «{label}» не построена: {exc}."
        warnings.append(msg)
        el.append(DocParagraph(msg, "note"))
        return
    if title:
        el.append(DocHeading(b.level, title))
    el.append(DocChart(label, png, label))
    if data.other_merged:
        el.append(DocParagraph(f"Категории вне первых {b.top_n} объединены в «Прочее» ({data.other_merged}).", "note"))


def _notes(ctx: BuildContext, doc: ReportDoc, fmt: Formatter, block: Block, warnings: list[str]) -> list:
    t = ctx.template
    out: list = [DocHeading(block.level, block.title or "Примечания")]
    items = [("Дата и время формирования", fmt.datetime(ctx.generated_at)),
             ("Сервер", ctx.server or "—"), ("База данных", ctx.database or "—"),
             ("Шаблон отчёта", t.name), ("Стиль оформления", doc.style.title)]
    if doc.author:
        items.append(("Автор", doc.author))
    if t.params:
        items.append(("Параметры", "; ".join(
            f"{p.label or p.name} = {ctx.param_values.get(p.name) if ctx.param_values.get(p.name) is not None else '—'}"
            for p in t.params)))
    items.append(("Лимит строк в таблице", fmt.number(ctx.max_rows, 0)))
    for sid, ds in ctx.datasets.items():
        items.append((f"Источник «{ds.name}»", f"{fmt.number(len(ds.rows), 0)} строк"
                      + (" (получена только часть)" if ds.truncated else "") + f"; {ds.elapsed:.2f} с"))
    if ctx.app_version:
        items.append(("Программа", f"DB Workspace Pro {ctx.app_version}"))
    out.append(DocKeyValues("", items))
    if t.include_sql:
        out.append(DocParagraph("Тексты запросов (пароли и токены скрыты):", "note"))
        for ds in ctx.datasets.values():
            out.append(DocParagraph(f"-- {ds.name}\n{ds.sql}", "code"))
    if warnings:
        out.append(DocParagraph("Предупреждения при формировании:", "note"))
        out.append(DocBullets(list(dict.fromkeys(warnings))))
    return out
