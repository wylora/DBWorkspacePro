"""Схема связей между таблицами (по внешним ключам): простая силовая раскладка + matplotlib, без внешних библиотек."""
from __future__ import annotations

import io
import math
import random

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

from comparison.schema import SchemaModel


def relationships(model: SchemaModel) -> list[tuple[str, str, str]]:
    """(таблица, ссылается_на, имя_ключа) — только связи между таблицами, которые есть в модели."""
    keys = {t.full.casefold(): t.full for t in model.tables.values()}
    out = []
    for t in model.tables.values():
        for fk in t.fks:
            target = keys.get(f"{fk.ref_schema}.{fk.ref_table}".casefold())
            if target:
                out.append((t.full, target, fk.name))
    return out


def _layout(nodes: list[str], edges: list[tuple[str, str]], iterations: int = 300) -> dict[str, tuple[float, float]]:
    rnd = random.Random(42)
    n = len(nodes)
    k = math.sqrt(1.0 / max(n, 1))
    pos = {v: [rnd.random(), rnd.random()] for v in nodes}
    t = 0.1
    for _ in range(iterations):
        disp = {v: [0.0, 0.0] for v in nodes}
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                d = math.hypot(dx, dy) or 0.01
                f = k * k / d
                disp[a][0] += dx / d * f
                disp[a][1] += dy / d * f
                disp[b][0] -= dx / d * f
                disp[b][1] -= dy / d * f
        for a, b in edges:
            if a == b:
                continue
            dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
            d = math.hypot(dx, dy) or 0.01
            f = d * d / k
            disp[a][0] -= dx / d * f
            disp[a][1] -= dy / d * f
            disp[b][0] += dx / d * f
            disp[b][1] += dy / d * f
        for v in nodes:
            dx, dy = disp[v]
            d = math.hypot(dx, dy) or 0.01
            pos[v][0] += dx / d * min(d, t)
            pos[v][1] += dy / d * min(d, t)
        t *= 0.985
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    w, h = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    return {v: ((p[0] - min(xs)) / w, (p[1] - min(ys)) / h) for v, p in pos.items()}


def er_diagram_png(model: SchemaModel, max_tables: int = 60) -> tuple[bytes | None, str]:
    rels = relationships(model)
    if not rels:
        return None, "Внешних ключей между таблицами нет — схема связей не строится."
    degree: dict[str, int] = {}
    for a, b, _ in rels:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    nodes = sorted(degree, key=lambda x: (-degree[x], x))
    note = ""
    if len(nodes) > max_tables:
        note = (f"На схеме показаны {max_tables} наиболее связанных таблиц из {len(nodes)}; "
                "полный список связей приведён в таблице ниже.")
        nodes = nodes[:max_tables]
    keep = set(nodes)
    edges = [(a, b) for a, b, _ in rels if a in keep and b in keep]
    pos = _layout(nodes, edges)
    n = len(nodes)
    size = max(7.0, min(22.0, math.sqrt(n) * 2.8))
    fig = Figure(figsize=(size, size * 0.72), dpi=130)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_xlim(-0.08, 1.08)
    ax.set_ylim(-0.08, 1.08)
    ax.axis("off")
    fs = 8 if n <= 25 else 6.5 if n <= 45 else 5.5
    for a, b in edges:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        if a == b:
            continue
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=9, color="#7A8AA0",
                                     linewidth=0.8, shrinkA=14, shrinkB=14, zorder=1))
    for v in nodes:
        x, y = pos[v]
        label = v[4:] if v.lower().startswith("dbo.") else v
        w = 0.012 * len(label) * fs / 8 + 0.02
        ax.add_patch(FancyBboxPatch((x - w / 2, y - 0.018), w, 0.036, boxstyle="round,pad=0.004,rounding_size=0.006",
                                    facecolor="#E8EEF7", edgecolor="#1F4E79", linewidth=0.9, zorder=2))
        ax.text(x, y, label, ha="center", va="center", fontsize=fs, color="#1F2937", zorder=3)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    return buf.getvalue(), note or "Стрелка ведёт от таблицы со ссылкой (внешним ключом) к таблице, на которую она ссылается."
