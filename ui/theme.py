"""Светлая и тёмная темы (единая таблица стилей QSS)."""
from __future__ import annotations

from string import Template

PALETTES: dict[str, dict[str, str]] = {
    "light": dict(
        bg="#F4F6FA", surface="#FFFFFF", surface_alt="#EEF1F7", border="#D6DCE8", text="#1F2937",
        muted="#6B7280", accent="#2563EB", accent_hover="#1D4ED8", accent_text="#FFFFFF",
        danger="#DC2626", success="#16A34A", warning="#D97706", sidebar_bg="#111827",
        sidebar_text="#CBD5E1", sidebar_muted="#6B7280", sidebar_active="#2563EB",
        selection="#DBEAFE", selection_text="#1E3A8A", input_bg="#FFFFFF"),
    "dark": dict(
        bg="#0F172A", surface="#1E293B", surface_alt="#273449", border="#334155", text="#E2E8F0",
        muted="#94A3B8", accent="#3B82F6", accent_hover="#60A5FA", accent_text="#FFFFFF",
        danger="#F87171", success="#4ADE80", warning="#FBBF24", sidebar_bg="#0B1220",
        sidebar_text="#CBD5E1", sidebar_muted="#64748B", sidebar_active="#3B82F6",
        selection="#1E3A5F", selection_text="#DBEAFE", input_bg="#0F172A"),
}

_QSS = Template("""
QWidget { background-color: $bg; color: $text; font-size: 13px; }
QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background-color: $bg; }
QLabel { background: transparent; }
QLabel#PageTitle { font-size: 22px; font-weight: 600; }
QLabel#PageSubtitle, QLabel#Muted { color: $muted; }
QLabel#CardValue { font-size: 26px; font-weight: 600; }
QLabel#CardTitle { color: $muted; font-size: 12px; }
QLabel#Badge { background-color: $surface_alt; color: $muted; border: 1px solid $border;
               border-radius: 8px; padding: 2px 8px; }
QFrame#Card { background-color: $surface; border: 1px solid $border; border-radius: 10px; }
QFrame#Card QLabel { background: transparent; }
QFrame#Card QWidget { background-color: transparent; }
QFrame#Card QLineEdit, QFrame#Card QComboBox, QFrame#Card QSpinBox,
QFrame#Card QPlainTextEdit, QFrame#Card QTableWidget, QFrame#Card QListWidget,
QFrame#Card QTreeWidget { background-color: $input_bg; }

QPushButton { background-color: $surface; border: 1px solid $border; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { border-color: $accent; }
QPushButton:disabled { color: $muted; background-color: $surface_alt; }
QPushButton#Primary { background-color: $accent; color: $accent_text; border: 1px solid $accent; }
QPushButton#Primary:hover { background-color: $accent_hover; }
QPushButton#Primary:disabled { background-color: $surface_alt; color: $muted; border-color: $border; }
QPushButton#Danger { color: $danger; }

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {
    background-color: $input_bg; border: 1px solid $border; border-radius: 6px; padding: 5px 8px;
    selection-background-color: $selection; selection-color: $selection_text; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: $accent; }
QComboBox QAbstractItemView { background-color: $surface; border: 1px solid $border;
    selection-background-color: $selection; selection-color: $selection_text; }
QCheckBox { spacing: 8px; background: transparent; }

QTreeWidget, QTableWidget, QListWidget { background-color: $surface; border: 1px solid $border;
    border-radius: 8px; alternate-background-color: $surface_alt; outline: 0; }
QTreeWidget::item, QListWidget::item { padding: 4px 6px; }
QTreeWidget::item:selected, QListWidget::item:selected, QTableWidget::item:selected {
    background-color: $selection; color: $selection_text; }
QHeaderView::section { background-color: $surface_alt; color: $muted; border: none;
    border-bottom: 1px solid $border; padding: 6px 8px; font-weight: 600; }
QTableCornerButton::section { background-color: $surface_alt; border: none; }

QListWidget#Sidebar { background-color: $sidebar_bg; border: none; border-radius: 0; padding-top: 8px; }
QListWidget#Sidebar::item { color: $sidebar_text; padding: 10px 16px; border-radius: 6px; margin: 1px 8px; }
QListWidget#Sidebar::item:hover { background-color: rgba(255,255,255,0.06); }
QListWidget#Sidebar::item:selected { background-color: $sidebar_active; color: #FFFFFF; }
QLabel#SidebarBrand { background-color: $sidebar_bg; color: #FFFFFF; font-size: 16px;
    font-weight: 700; padding: 18px 16px 8px 16px; }
QLabel#SidebarVersion { background-color: $sidebar_bg; color: $sidebar_muted; padding: 6px 16px 12px 16px; }

QSplitter::handle { background-color: $border; }
QStatusBar { background-color: $surface; border-top: 1px solid $border; }
QStatusBar QLabel { background: transparent; padding: 0 6px; }
QProgressBar { border: 1px solid $border; border-radius: 4px; background: $surface_alt; height: 8px; }
QProgressBar::chunk { background-color: $accent; border-radius: 3px; }
QScrollBar:vertical { background: transparent; width: 12px; }
QScrollBar::handle:vertical { background: $border; border-radius: 5px; min-height: 24px; }
QScrollBar:horizontal { background: transparent; height: 12px; }
QScrollBar::handle:horizontal { background: $border; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QMenu { background-color: $surface; border: 1px solid $border; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 4px; }
QMenu::item:selected { background-color: $selection; color: $selection_text; }
QToolTip { background-color: $surface; color: $text; border: 1px solid $border; padding: 4px; }
QMessageBox, QDialog { background-color: $bg; }
""")


def colors(name: str) -> dict[str, str]:
    return PALETTES.get(name, PALETTES["light"])


def build_stylesheet(name: str) -> str:
    return _QSS.substitute(colors(name))


def apply_theme(app, name: str) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet(name))
