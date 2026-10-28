"""Отчёты о сравнении (Word / Excel / HTML) через общие рендереры."""
from __future__ import annotations

from reports.adhoc import make_doc, table
from reports.document import DocBullets, DocHeading, DocKeyValues, DocParagraph
from reports.formatting import Formatter

from .data_compare import DataCompareResult
from .schema_diff import CATEGORY_RU, STATUS_RU, SchemaDiff

MAX_ROWS = 5000


def schema_diff_doc(d: SchemaDiff, source_label: str, target_label: str):
    els: list = [DocHeading(1, "Итог"), DocParagraph(d.summary()),
                 DocKeyValues("", [("Исходная база", source_label), ("Целевая база", target_label),
                                   ("Таблиц без различий", str(d.equal_tables)),
                                   ("Модулей без различий", str(d.equal_modules)),
                                   ("Только в исходной", str(d.count("only_source"))),
                                   ("Только в целевой", str(d.count("only_target"))),
                                   ("Различается", str(d.count("changed"))),
                                   ("Сравнить нельзя", str(d.count("unknown")))])]
    for status in ("only_source", "only_target", "changed", "unknown"):
        items = [x for x in d.items if x.status == status]
        if not items:
            continue
        els.append(DocHeading(1, STATUS_RU[status].capitalize()))
        els.append(table("", ["Вид", "Объект", "Подробности"],
                         [[CATEGORY_RU.get(x.category, x.category), x.object, "; ".join(x.details)] for x in items[:MAX_ROWS]],
                         note=f"Показаны первые {MAX_ROWS}." if len(items) > MAX_ROWS else ""))
    if d.notes:
        els += [DocHeading(1, "Ограничения"), DocBullets(d.notes)]
    return make_doc("Сравнение структуры баз данных", els, subtitle=f"{source_label} → {target_label}", toc=False,
                    orientation="landscape")


def data_diff_doc(r: DataCompareResult):
    fmt = Formatter("ru")
    d, req = r.diff, r.request
    els: list = [DocHeading(1, "Итог"), DocParagraph(d.summary()),
                 DocKeyValues("", [("Исходная таблица", req.source.label), ("Целевая таблица", req.target.label),
                                   ("Ключ", ", ".join(req.key_columns)),
                                   ("Строк в исходной", fmt.number(r.source_rows, 0)),
                                   ("Строк в целевой", fmt.number(r.target_rows, 0)),
                                   ("Сравнивались столбцы", ", ".join(d.compared_columns) or "—")])]
    if d.totals:
        els += [DocHeading(2, "Суммы числовых столбцов"),
                table("", ["Столбец", "В целевой", "В исходной", "Разница"],
                      [[c, a, b, b - a] for c, (a, b) in d.totals.items()], aligns=["left", "right", "right", "right"])]
    for title, rows, total in (("Есть только в исходной (новые)", d.added, d.added_total),
                               ("Есть только в целевой (удалённые)", d.removed, d.removed_total)):
        if rows:
            els += [DocHeading(1, title),
                    table("", r.columns if title.startswith("Есть только в исходной") else r.columns, [list(x) for x in rows],
                          note=f"Показано {len(rows)} из {total}." if total > len(rows) else "")]
    if d.changed:
        rows = [[", ".join(fmt.value(k) for k in ch.key), col, old, new] for ch in d.changed for col, (old, new) in ch.changes.items()]
        els += [DocHeading(1, "Изменённые значения"), table("", ["Ключ", "Столбец", "В целевой", "В исходной"], rows,
                note=f"Показано {len(d.changed)} из {d.changed_total} изменённых строк." if d.changed_total > len(d.changed) else "")]
    if r.notes:
        els += [DocHeading(1, "Примечания"), DocBullets(r.notes)]
    return make_doc("Сравнение данных таблиц", els, subtitle=f"{req.source.label} → {req.target.label}", toc=False,
                    orientation="landscape")
