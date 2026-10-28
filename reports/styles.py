"""Шесть готовых стилей оформления отчётов."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StylePreset:
    id: str
    title: str
    description: str
    font_name: str
    font_size: float
    table_font_name: str
    table_font_size: float
    heading_font_name: str
    heading_sizes: tuple[float, float, float]
    heading_color: str
    accent: str
    header_bg: str
    header_fg: str
    zebra: str            # пустая строка — без чередования
    border: str
    muted: str
    table_style: str      # grid | horizontal | rules (академические «три линии») | minimal
    line_spacing: float
    title_align: str      # left | center
    margin_cm: float
    palette: tuple[str, ...]
    totals_double_rule: bool = False


STYLES: dict[str, StylePreset] = {s.id: s for s in (
    StylePreset("business", "Деловой", "Строгий деловой стиль: синие заголовки, сетка таблиц, чередование строк.",
                "Calibri", 11, "Calibri", 10, "Calibri", (20, 15, 12.5), "#1F4E79", "#1F4E79", "#1F4E79", "#FFFFFF",
                "#F2F6FA", "#BFC9D6", "#6B7280", "grid", 1.15, "left", 2.0,
                ("#1F4E79", "#2E75B6", "#9DC3E6", "#F4B183", "#A9D18E", "#FFD966", "#8497B0", "#C55A11")),
    StylePreset("academic", "Академический", "Шрифт с засечками, полуторный интервал, таблицы «тремя линиями».",
                "Times New Roman", 12, "Times New Roman", 11, "Times New Roman", (16, 14, 12), "#000000", "#000000",
                "#FFFFFF", "#000000", "", "#000000", "#555555", "rules", 1.5, "center", 2.5,
                ("#000000", "#555555", "#888888", "#AAAAAA", "#CCCCCC", "#333333", "#777777", "#999999")),
    StylePreset("minimal", "Минималистичный", "Много воздуха, тонкие линии, без цветных заливок.",
                "Arial", 10.5, "Arial", 9.5, "Arial", (18, 13, 11), "#222222", "#444444", "#FFFFFF", "#222222",
                "", "#D0D0D0", "#808080", "minimal", 1.2, "left", 2.2,
                ("#444444", "#888888", "#BBBBBB", "#666666", "#999999", "#333333", "#AAAAAA", "#CCCCCC")),
    StylePreset("analytic", "Аналитический", "Бирюзовые акценты, плотные таблицы, акцент на цифры и диаграммы.",
                "Calibri", 10.5, "Calibri", 9.5, "Calibri", (20, 14, 12), "#0F766E", "#0F766E", "#0F766E", "#FFFFFF",
                "#F0FDFA", "#B6DAD6", "#5B6B6A", "grid", 1.1, "left", 1.8,
                ("#0F766E", "#6366F1", "#F59E0B", "#EF4444", "#10B981", "#8B5CF6", "#EC4899", "#64748B")),
    StylePreset("technical", "Технический", "Серый контрастный стиль, моноширинный шрифт в таблицах.",
                "Arial", 10, "Consolas", 9, "Arial", (18, 14, 12), "#374151", "#374151", "#374151", "#FFFFFF",
                "#F3F4F6", "#9CA3AF", "#6B7280", "grid", 1.1, "left", 1.8,
                ("#374151", "#2563EB", "#DC2626", "#059669", "#D97706", "#7C3AED", "#0891B2", "#9CA3AF")),
    StylePreset("financial", "Финансовый", "Тёмно-зелёные акценты, выделенная строка итогов, числа справа.",
                "Cambria", 11, "Calibri", 10, "Cambria", (20, 15, 12.5), "#14532D", "#14532D", "#14532D", "#FFFFFF",
                "#F4F9F4", "#A7C4AE", "#4B5563", "horizontal", 1.15, "left", 2.0,
                ("#14532D", "#16A34A", "#86EFAC", "#CA8A04", "#B45309", "#475569", "#0E7490", "#7F1D1D"),
                True),
)}


def get_style(style_id: str) -> StylePreset:
    return STYLES.get(style_id, STYLES["business"])
