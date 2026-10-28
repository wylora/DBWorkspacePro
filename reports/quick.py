"""Быстрый отчёт по сохранённому запросу (из «Избранного» SQL-редактора): код запроса и его результат."""
from __future__ import annotations

from .model import Block, DataSource, Param, ReportTemplate
from .variables import find_named_params

FAV_PREFIX = "fav:"


def template_from_query(title: str, sql: str, *, key: str = "query") -> ReportTemplate:
    """Шаблон из одного запроса: блок «Код запроса и результат» и сведения о формировании.

    Параметры :имя из текста запроса становятся параметрами отчёта (их заполняют перед формированием).
    Как и все отчёты, запрос должен только читать данные — иначе шаблон не пройдёт проверку.
    """
    title = (title or "Запрос").strip()
    params = [Param(n, n, "text", "", True) for n in find_named_params(sql)]
    return ReportTemplate(
        id=f"{FAV_PREFIX}{key}", name=f"Запрос: {title}", title=title, subtitle="Код запроса и результат выполнения",
        style="technical", title_page=False, toc=False, header_text=title,
        description="Отчёт по сохранённому запросу из «Избранного» SQL-редактора.",
        params=params,
        sources=[DataSource(id="q", name=title, kind="sql", sql=sql)],
        blocks=[Block(id="query", type="query", title="", source="q", level=1),
                Block(id="notes", type="notes", title="Сведения о формировании", level=1)])


def appendix_blocks(t: ReportTemplate) -> list[Block]:
    """Блоки приложения «Код запросов и результаты» для всех источников шаблона."""
    blocks = [Block(id="appendix-h", type="heading", title="Приложение. Код запросов и результаты", level=1)]
    for s in t.sources:
        blocks.append(Block(id=f"appendix-{s.id}", type="query", title=f"Запрос «{s.name or s.id}»", source=s.id, level=2))
    return blocks
