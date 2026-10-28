"""Диаграммы (matplotlib, без GUI) и проверка применимости к данным."""
from __future__ import annotations

import io
from dataclasses import dataclass

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from .formatting import Formatter
from .stats import (DATE, DATETIME, INTEGER, NUMERIC, TEXT, BOOL, aggregate_by, infer_kind, time_series, _is_num)
from .styles import StylePreset


class ChartNotApplicable(Exception):
    """Диаграмма не имеет смысла для этих данных; текст — причина для пользователя."""


@dataclass
class ChartData:
    labels: list[str]
    series: dict[str, list[float]]
    time_axis: bool = False
    other_merged: int = 0


def _f(v) -> float:
    return float(v)


def chart_data(kind: str, columns: list[str], rows: list[tuple], x: str, ys: list[str], agg: str,
               top_n: int, fmt: Formatter) -> ChartData:
    if not rows:
        raise ChartNotApplicable("нет данных")
    idx = {c: i for i, c in enumerate(columns)}
    if x not in idx:
        raise ChartNotApplicable(f"в источнике нет столбца «{x}»")
    xi = idx[x]
    xkind = infer_kind([r[xi] for r in rows])
    ys = [y for y in ys] if agg != "count" else []
    for y in ys:
        if y not in idx:
            raise ChartNotApplicable(f"в источнике нет столбца «{y}»")
        if infer_kind([r[idx[y]] for r in rows]) not in (NUMERIC, INTEGER):
            raise ChartNotApplicable(f"столбец «{y}» не числовой")
    if agg != "count" and not ys:
        raise ChartNotApplicable("не выбран числовой столбец")
    if kind == "pie" and len(ys) > 1:
        ys = ys[:1]
    targets = ys or [None]

    time_axis = xkind in (DATE, DATETIME)
    if kind == "line" and xkind not in (DATE, DATETIME, INTEGER, NUMERIC):
        raise ChartNotApplicable("линейный график строится по датам или числам (ось X — текст не подходит)")
    if kind == "pie" and time_axis:
        raise ChartNotApplicable("круговая диаграмма не подходит для дат — используйте столбчатую или линейную")
    if xkind not in (DATE, DATETIME, INTEGER, NUMERIC, TEXT, BOOL):
        raise ChartNotApplicable("тип столбца X не поддерживается для диаграмм")

    series: dict[str, list[float]] = {}
    labels: list[str] = []
    other = 0
    if time_axis:
        base = None
        for y in targets:
            pts = time_series(rows, xi, idx[y] if y else None, agg)
            pts = [(l, v) for l, v in pts if v is not None]
            if len(pts) < 2 and kind == "line":
                raise ChartNotApplicable("для линейного графика нужно минимум два периода")
            if base is None:
                base = [l for l, _ in pts]
            d = dict(pts)
            series[y or "Количество"] = [_f(d.get(l, 0)) for l in base]
        labels = base or []
        if len(labels) > 36:
            labels, series = labels[-36:], {k: v[-36:] for k, v in series.items()}
    else:
        first = None
        for y in targets:
            pairs = [(k, v) for k, v in aggregate_by(rows, xi, idx[y] if y else None, agg) if v is not None]
            if first is None:
                if kind == "line":
                    pairs.sort(key=lambda p: _f(p[0]) if _is_num(p[0]) else 0)
                else:
                    pairs.sort(key=lambda p: _f(p[1]), reverse=True)
                if kind == "pie":
                    if any(_f(v) < 0 for _, v in pairs):
                        raise ChartNotApplicable("круговая диаграмма не подходит для отрицательных значений")
                    pairs = [(k, v) for k, v in pairs if _f(v) > 0]
                    if len(pairs) < 2:
                        raise ChartNotApplicable("в данных меньше двух категорий с положительными значениями")
                    if len(pairs) > top_n:
                        rest = pairs[top_n:]
                        pairs = pairs[:top_n] + [("Прочее", sum(_f(v) for _, v in rest))]
                        other = len(rest)
                elif len(pairs) > top_n:
                    pairs = pairs[:top_n]
                first = [k for k, _ in pairs]
                labels = [fmt.value(k) if not isinstance(k, str) else k for k in first]
            d = dict(pairs) if first else {}
            if first is not None and y is not targets[0]:
                d = {k: v for k, v in aggregate_by(rows, xi, idx[y] if y else None, agg) if v is not None}
            series[y or "Количество"] = [_f(d.get(k, 0)) for k in first]
        if len(labels) < 1:
            raise ChartNotApplicable("нет данных для построения")
    return ChartData(labels, series, time_axis, other)


def render_chart(data: ChartData, kind: str, title: str, style: StylePreset, fmt: Formatter,
                 width_in: float = 6.5, height_in: float = 3.6) -> bytes:
    fig = Figure(figsize=(width_in, height_in), dpi=150)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    colors = list(style.palette)
    names = list(data.series)
    n = len(data.labels)
    tick = FuncFormatter(lambda v, _p: fmt.number(v, 0 if abs(v) >= 10 or float(v).is_integer() else 2))
    if title:
        ax.set_title(title, fontsize=11, color=style.heading_color, pad=10, loc="left")
    if kind == "pie":
        vals = data.series[names[0]]
        ax.pie(vals, labels=None, colors=[colors[i % len(colors)] for i in range(n)], startangle=90,
               counterclock=False, wedgeprops={"linewidth": 1, "edgecolor": "white"},
               autopct=lambda p: f"{p:.0f}%" if p >= 4 else "", pctdistance=0.78, textprops={"fontsize": 8, "color": "white"})
        total = sum(vals) or 1
        ax.legend([f"{l} — {fmt.number(v, 0 if float(v).is_integer() else 2)} ({v / total * 100:.0f}%)"
                   for l, v in zip(data.labels, vals)], loc="center left", bbox_to_anchor=(1.0, 0.5),
                  fontsize=8, frameon=False)
        ax.axis("equal")
    elif kind == "line":
        for i, name in enumerate(names):
            ax.plot(range(n), data.series[name], marker="o", markersize=3.5, linewidth=1.8,
                    color=colors[i % len(colors)], label=name)
        ax.set_xticks(range(n))
        step = max(1, n // 12)
        ax.set_xticklabels([l if i % step == 0 else "" for i, l in enumerate(data.labels)], fontsize=8,
                           rotation=35 if n > 6 else 0, ha="right" if n > 6 else "center")
        ax.yaxis.set_major_formatter(tick)
        ax.grid(axis="y", color="#DDDDDD", linewidth=0.6)
    else:
        horizontal = len(names) == 1 and (n > 8 or max((len(str(l)) for l in data.labels), default=0) > 14)
        k = len(names)
        width = 0.8 / k
        if horizontal:
            pos = list(range(n))[::-1]
            for i, name in enumerate(names):
                bars = ax.barh([p + i * width for p in pos], data.series[name], height=width,
                               color=colors[i % len(colors)], label=name)
                if n <= 15:
                    for b, v in zip(bars, data.series[name]):
                        ax.text(b.get_width(), b.get_y() + b.get_height() / 2, " " + fmt.number(v, 0 if float(v).is_integer() else 2),
                                va="center", fontsize=7, color="#444444")
            ax.set_yticks([p + width * (k - 1) / 2 for p in pos])
            ax.set_yticklabels([str(l)[:32] for l in data.labels], fontsize=8)
            ax.xaxis.set_major_formatter(tick)
            ax.grid(axis="x", color="#DDDDDD", linewidth=0.6)
        else:
            pos = list(range(n))
            for i, name in enumerate(names):
                bars = ax.bar([p + i * width for p in pos], data.series[name], width=width,
                              color=colors[i % len(colors)], label=name)
                if n <= 12 and k == 1:
                    for b, v in zip(bars, data.series[name]):
                        ax.text(b.get_x() + b.get_width() / 2, b.get_height(), fmt.number(v, 0 if float(v).is_integer() else 2),
                                ha="center", va="bottom", fontsize=7, color="#444444")
            ax.set_xticks([p + width * (k - 1) / 2 for p in pos])
            ax.set_xticklabels([str(l)[:18] for l in data.labels], fontsize=8,
                               rotation=30 if n > 5 else 0, ha="right" if n > 5 else "center")
            ax.yaxis.set_major_formatter(tick)
            ax.grid(axis="y", color="#DDDDDD", linewidth=0.6)
        ax.set_axisbelow(True)
    if kind != "pie":
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(axis="both", labelsize=8)
        if len(names) > 1:
            ax.legend(fontsize=8, frameon=False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    return buf.getvalue()
