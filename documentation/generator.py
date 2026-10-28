"""Генератор документации базы данных (Word, PDF, HTML, Markdown) со схемой связей."""
from __future__ import annotations

from dataclasses import dataclass, field

from comparison.schema import Module, SchemaModel, Table
from comparison.schema_diff import CATEGORY_RU, compare_schemas
from reports.adhoc import make_doc, table
from reports.document import DocBullets, DocChart, DocHeading, DocKeyValues, DocParagraph
from reports.formatting import Formatter

from .diagram import er_diagram_png, relationships


@dataclass
class DocOptions:
    title: str = ""
    organization: str = ""
    author: str = ""
    style: str = "technical"
    include_definitions: bool = True
    include_diagram: bool = True
    include_indexes: bool = True
    include_changes: bool = True
    max_diagram_tables: int = 60
    max_definition_lines: int = 400


def _key_marks(t: Table) -> dict[str, list[str]]:
    marks: dict[str, list[str]] = {}
    if t.pk:
        for c in t.pk.columns:
            marks.setdefault(c.replace(" DESC", "").casefold(), []).append("PK")
    for fk in t.fks:
        for c in fk.columns:
            marks.setdefault(c.casefold(), []).append(f"FK → {fk.ref_table}")
    return marks


def _definition(m: Module, opts: DocOptions, els: list) -> None:
    if not opts.include_definitions:
        return
    if m.definition is None:
        els.append(DocParagraph("Определение недоступно (нет права VIEW DEFINITION или объект зашифрован).", "note"))
        return
    lines = m.definition.replace("\r\n", "\n").strip().split("\n")
    text = "\n".join(lines[: opts.max_definition_lines])
    if len(lines) > opts.max_definition_lines:
        text += f"\n-- … ещё {len(lines) - opts.max_definition_lines} строк"
    els.append(DocParagraph(text, "code"))


def build_documentation(model: SchemaModel, opts: DocOptions, previous: SchemaModel | None = None):
    fmt = Formatter("ru")
    els: list = []
    tables = sorted(model.tables.values(), key=lambda t: t.full.casefold())
    mods = sorted(model.modules.values(), key=lambda m: (m.kind, m.full.casefold()))
    views = [m for m in mods if m.kind == "view"]
    procs = [m for m in mods if m.kind == "procedure"]
    funcs = [m for m in mods if m.kind == "function"]
    trigs = [m for m in mods if m.kind == "trigger"]
    rels = relationships(model)

    # ---- обзор
    els.append(DocHeading(1, "Обзор"))
    els.append(DocParagraph(
        f"База данных «{model.database}»: таблиц — {len(tables)}, столбцов — {sum(len(t.columns) for t in tables)}, "
        f"связей — {len(rels)}, представлений — {len(views)}, процедур — {len(procs)}, функций — {len(funcs)}, "
        f"триггеров — {len(trigs)}."))
    els.append(DocParagraph("В документацию попадают только объекты, метаданные которых доступны учётной записи, "
                            "под которой она сформирована.", "note"))
    if tables:
        els.append(table("Таблицы", ["Таблица", "Строк (прибл.)", "Столбцов", "Описание"],
                         [[t.full, t.rows if t.rows is not None else "", len(t.columns), t.description] for t in tables],
                         aligns=["left", "right", "right", "left"]))
    # ---- изменения
    if opts.include_changes and previous is not None:
        d = compare_schemas(model, previous)
        els.append(DocHeading(1, "Изменения с прошлой версии документации"))
        if d.identical:
            els.append(DocParagraph(f"По сравнению со снимком от {previous.read_at.replace('T', ' ')} структура не "
                                    "изменилась."))
        else:
            word = {"only_source": "добавлено", "only_target": "удалено", "changed": "изменено", "unknown": "сравнить нельзя"}
            els.append(DocParagraph(f"Сравнение со снимком от {previous.read_at.replace('T', ' ')}."))
            els.append(table("", ["Вид", "Объект", "Изменение", "Подробности"],
                             [[CATEGORY_RU.get(x.category, x.category), x.object, word[x.status], "; ".join(x.details)]
                              for x in d.items]))
    # ---- схема связей
    if rels:
        els.append(DocHeading(1, "Схема связей"))
        if opts.include_diagram:
            png, note = er_diagram_png(model, opts.max_diagram_tables)
            if png:
                els.append(DocChart("Схема связей между таблицами", png, "Схема связей между таблицами"))
            els.append(DocParagraph(note, "note"))
        rows = []
        for t in tables:
            for fk in t.fks:
                rows.append([t.full, ", ".join(fk.columns), f"{fk.ref_schema}.{fk.ref_table}", ", ".join(fk.ref_columns),
                             f"{fk.on_delete.replace('_', ' ')} / {fk.on_update.replace('_', ' ')}" +
                             (" (отключён)" if fk.disabled else "")])
        els.append(table("Связи (внешние ключи)", ["Таблица", "Столбцы", "Ссылается на", "Столбцы цели", "Удаление / изменение"], rows))
    # ---- таблицы
    if tables:
        els.append(DocHeading(1, "Таблицы"))
        incoming: dict[str, list[str]] = {}
        for a, b, name in rels:
            incoming.setdefault(b, []).append(f"{a} ({name})")
        for t in tables:
            els.append(DocHeading(2, t.full))
            if t.description:
                els.append(DocParagraph(t.description))
            marks = _key_marks(t)
            els.append(table("Столбцы", ["№", "Столбец", "Тип", "NULL", "По умолчанию", "Ключ", "Описание"],
                             [[c.ordinal, c.name, c.type + (" IDENTITY" if c.identity else "") +
                               (f" AS {c.computed}" if c.computed else ""), "да" if c.nullable else "нет", c.default,
                               ", ".join(marks.get(c.name.casefold(), [])), c.description]
                              for c in sorted(t.columns, key=lambda c: c.ordinal)],
                             aligns=["right", "left", "left", "center", "left", "left", "left"], compact=True))
            if opts.include_indexes and t.indexes:
                els.append(table("Индексы и ограничения", ["Имя", "Вид", "Уникальный", "Столбцы", "Включённые", "Фильтр"],
                                 [[i.name, ("PRIMARY KEY " if i.primary else "UNIQUE CONSTRAINT " if i.unique_constraint
                                            else "") + i.type_desc, "да" if i.unique else "нет", ", ".join(i.columns),
                                   ", ".join(i.included), i.filter] for i in t.indexes], compact=True))
            if incoming.get(t.full):
                els.append(DocParagraph("На эту таблицу ссылаются: " + "; ".join(incoming[t.full]) + "."))
            t_trigs = [m.name for m in trigs if m.parent.casefold() == t.full.casefold()]
            if t_trigs:
                els.append(DocParagraph("Триггеры: " + ", ".join(t_trigs) + "."))
    # ---- модули
    for title, items in (("Представления", views), ("Хранимые процедуры", procs), ("Функции", funcs)):
        if not items:
            continue
        els.append(DocHeading(1, title))
        for m in items:
            els.append(DocHeading(2, m.full))
            if m.description:
                els.append(DocParagraph(m.description))
            if m.kind == "view" and m.columns:
                els.append(table("Столбцы", ["Столбец", "Тип", "NULL"],
                                 [[c.name, c.type, "да" if c.nullable else "нет"] for c in m.columns], compact=True))
            params = [p for p in m.params if p.ordinal > 0]
            ret = next((p for p in m.params if p.ordinal == 0), None)
            if ret is not None:
                els.append(DocParagraph(f"Возвращает: {ret.type}"))
            if params:
                els.append(table("Параметры", ["Параметр", "Тип", "Выходной"],
                                 [[p.name, p.type, "да" if p.output else ""] for p in params], compact=True))
            elif m.kind in ("procedure", "function"):
                els.append(DocParagraph("Параметров нет.", "note"))
            _definition(m, opts, els)
    if trigs:
        els.append(DocHeading(1, "Триггеры"))
        els.append(table("", ["Триггер", "Таблица / уровень", "Вид"], [[m.full, m.parent, m.type_desc] for m in trigs]))
        if opts.include_definitions:
            for m in trigs:
                els.append(DocHeading(2, m.full))
                _definition(m, opts, els)
    # ---- примечания
    els.append(DocHeading(1, "Сведения о формировании"))
    els.append(DocKeyValues("", [("Сервер", model.server or "—"), ("База данных", model.database),
                                 ("Структура прочитана", model.read_at.replace("T", " ")),
                                 ("Определения модулей", "включены" if opts.include_definitions else "не включались")]))
    if model.warnings:
        els.append(DocParagraph("Ограничения доступа и недоступные сведения:", "note"))
        els.append(DocBullets(list(model.warnings)))
    title = opts.title or f"Документация базы данных «{model.database}»"
    return make_doc(title, els, style=opts.style, subtitle=f"Сервер: {model.server}" if model.server else "",
                    author=opts.author, organization=opts.organization, toc=True)
