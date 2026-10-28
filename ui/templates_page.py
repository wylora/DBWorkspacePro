"""Конструктор шаблонов отчётов: общие настройки, параметры, источники данных, блоки."""
from __future__ import annotations

import logging

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget,
                               QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from database.errors import DbWorkspaceError
from reports.data import fetch_dataset
from reports.model import (BLOCK_TITLES, BLOCK_TYPES, Aggregate, Block, ColumnSpec, ComputedColumn, DataSource, Filter,
                           Metric, Param, ReportTemplate, SortSpec, new_id, validate_template)
from reports.query_builder import build_query
from reports.styles import STYLES
from reports.templates import TemplateError
from reports.variables import ParamError, coerce_params
from utils.formatting import format_cell

from .code_editor import CodeEditor
from .context import AppContext
from .table_editor import Col, TableEditor
from .widgets import PageHeader

log = logging.getLogger("dbworkspace.templates")

STYLE_CHOICES = [(s.title, s.id) for s in STYLES.values()]
ORIENT = [("Книжная", "portrait"), ("Альбомная", "landscape")]
LOCALES = [("Русский", "ru"), ("English", "en")]
PARAM_TYPES_RU = [("Текст", "text"), ("Целое число", "int"), ("Число", "decimal"), ("Дата", "date"), ("Да/нет", "bool")]
KINDS_RU = [("SQL-запрос", "sql"), ("Таблица или представление", "table"), ("Хранимая процедура", "procedure")]
OPS_RU = [("=", "="), ("<>", "<>"), ("<", "<"), (">", ">"), ("<=", "<="), (">=", ">="), ("LIKE", "like"),
          ("IN (значения через ;)", "in"), ("пусто (IS NULL)", "is_null"), ("не пусто", "not_null")]
AGG_RU = [("SUM", "sum"), ("AVG", "avg"), ("MIN", "min"), ("MAX", "max"), ("COUNT", "count"),
          ("COUNT DISTINCT", "count_distinct")]
FORMATS_RU = [("авто", "auto"), ("текст", "text"), ("целое", "integer"), ("число", "decimal"), ("валюта", "currency"),
              ("процент", "percent"), ("дата", "date"), ("дата и время", "datetime")]
TOTALS_RU = [("нет", "none"), ("сумма", "sum"), ("среднее", "avg"), ("количество", "count")]
ALIGN_RU = [("авто", "auto"), ("слева", "left"), ("справа", "right"), ("по центру", "center")]
COMPUTED_RU = [("A + B", "add"), ("A − B", "sub"), ("A × B", "mul"), ("A ÷ B", "div"), ("A ÷ B × 100 (%)", "pct")]
METRICS_RU = [("сумма", "sum"), ("среднее", "avg"), ("мин.", "min"), ("макс.", "max"), ("количество", "count"),
              ("уникальных", "distinct"), ("отношение сумм A/B", "ratio")]
CHART_KINDS_RU = [("Столбчатая", "bar"), ("Линейная", "line"), ("Круговая", "pie")]
CHART_AGGS_RU = [("сумма", "sum"), ("среднее", "avg"), ("количество строк", "count"), ("мин.", "min"), ("макс.", "max")]
BLOCKS_WITH_SOURCE = ("table", "query", "stats", "chart", "summary")


def _combo(items: list[tuple[str, str]]) -> QComboBox:
    c = QComboBox()
    for text, val in items:
        c.addItem(text, val)
    return c


def parse_sort(text: str) -> list[SortSpec]:
    out = []
    for part in text.split(","):
        bits = part.strip().rsplit(" ", 1)
        if not part.strip():
            continue
        if len(bits) == 2 and bits[1].lower() in ("desc", "asc", "убыв", "возр"):
            out.append(SortSpec(bits[0].strip(), bits[1].lower() in ("desc", "убыв")))
        else:
            out.append(SortSpec(part.strip(), False))
    return out


def format_sort(specs: list[SortSpec]) -> str:
    return ", ".join(s.column + (" desc" if s.desc else "") for s in specs)


def split_list(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


class PreviewDialog(QDialog):
    def __init__(self, parent, title: str, columns: list[str], rows: list[tuple], note: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 420)
        lay = QVBoxLayout(self)
        if note:
            lbl = QLabel(note)
            lbl.setWordWrap(True)
            lay.addWidget(lbl)
        table = QTableWidget(len(rows), len(columns))
        table.setHorizontalHeaderLabels(columns)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(format_cell(v, 200)))
        table.resizeColumnsToContents()
        lay.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


class TemplatesPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self.store = ctx.reports.templates
        self.t = self._new_template()
        self.sources: list[DataSource] = []
        self.blocks: list[Block] = []
        self._src_idx = -1
        self._blk_idx = -1
        self._columns: dict[str, list[str]] = {}
        self._loading = False
        self._dirty = False
        self._build()
        ctx.connections_changed.connect(self._reload_connections)
        ctx.open_template.connect(self.select_template)
        self._reload_connections()
        self._reload_list()
        self.load(self.t)

    # ====================================================== построение UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Конструктор шаблонов",
            "Шаблон описывает отчёт: оформление, параметры, источники данных (SQL, таблицы, процедуры) и блоки. "
            "Шаблоны хранятся в JSON; встроенные примеры можно только копировать."))
        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        left = QFrame()
        left.setObjectName("Card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_list_row)
        ll.addWidget(self.list, 1)
        for text, fn in (("＋ Новый", self.new_template), ("Копия", self.duplicate), ("Удалить", self.delete),
                         ("Импорт…", self.import_file), ("Экспорт…", self.export_file)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            ll.addWidget(b)
        left.setMinimumWidth(210)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.b_save = QPushButton("Сохранить")
        self.b_save.setObjectName("Primary")
        self.b_save.clicked.connect(self.save)
        b_check = QPushButton("Проверить шаблон")
        b_check.clicked.connect(self.check)
        self.c_conn = QComboBox()
        self.c_conn.setMinimumWidth(180)
        self.c_conn.setToolTip("Подключение для проверки источников данных")
        self.dirty_label = QLabel("")
        self.dirty_label.setObjectName("Muted")
        for w in (self.b_save, b_check, QLabel("Подключение для проверки:"), self.c_conn, self.dirty_label):
            bar.addWidget(w)
        bar.addStretch(1)
        rl.addLayout(bar)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_general(), "Общие")
        self.tabs.addTab(self._tab_params(), "Параметры")
        self.tabs.addTab(self._tab_sources(), "Источники данных")
        self.tabs.addTab(self._tab_blocks(), "Блоки")
        rl.addWidget(self.tabs, 1)
        split.addWidget(right)
        split.setSizes([230, 900])

    def _scroll(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(inner)
        return sa

    def _watch(self, w) -> None:
        for sig in ("textChanged", "toggled", "valueChanged", "currentIndexChanged", "changed"):
            s = getattr(w, sig, None)
            if s is not None:
                s.connect(self._mark_dirty)

    def _mark_dirty(self, *_a) -> None:
        if not self._loading:
            self._dirty = True
            self.dirty_label.setText("● есть несохранённые изменения")

    # ---- вкладка «Общие» ------------------------------------------------
    def _tab_general(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        f = QFormLayout(card)
        f.setContentsMargins(16, 14, 16, 14)
        f.setVerticalSpacing(8)
        self.g_name = QLineEdit()
        self.g_desc = QLineEdit()
        self.g_style = _combo(STYLE_CHOICES)
        self.g_style.setToolTip("\n".join(f"{s.title}: {s.description}" for s in STYLES.values()))
        self.g_font = QComboBox()
        self.g_font.setEditable(True)
        self.g_font.addItem("(шрифт стиля)", "")
        for fam in ("Calibri", "Cambria", "Arial", "Times New Roman", "Segoe UI", "Verdana", "Georgia", "Consolas"):
            self.g_font.addItem(fam, fam)
        self.g_size = QSpinBox()
        self.g_size.setRange(0, 24)
        self.g_size.setSpecialValueText("размер стиля")
        self.g_size.setSuffix(" пт")
        self.g_accent = QLineEdit()
        self.g_accent.setPlaceholderText("#RRGGBB — пусто: цвет стиля")
        b_color = QPushButton("Выбрать…")
        b_color.clicked.connect(self._pick_color)
        arow = QHBoxLayout()
        arow.addWidget(self.g_accent, 1)
        arow.addWidget(b_color)
        self.g_orient = _combo(ORIENT)
        self.g_margin = QSpinBox()
        self.g_margin.setRange(1, 6)
        self.g_margin.setSuffix(" см")
        self.g_locale = _combo(LOCALES)
        self.g_currency = QLineEdit()
        self.g_currency.setPlaceholderText("например, ₽ — для столбцов с форматом «валюта»")
        self.g_title = QLineEdit()
        self.g_title.setToolTip("Переменные: {{report_name}}, {{date}}, {{datetime}}, {{database}}, {{server}}, "
                                "{{param.ИМЯ}}, {{rows.ИСТОЧНИК}}, {{stat.ИСТОЧНИК.Столбец.sum}}")
        self.g_subtitle = QLineEdit()
        self.g_org = QLineEdit()
        self.g_author = QLineEdit()
        self.g_header = QLineEdit()
        self.g_footer = QLineEdit()
        self.k_titlepage = QCheckBox("Титульная страница")
        self.k_toc = QCheckBox("Оглавление (поле Word)")
        self.k_pages = QCheckBox("Номера страниц (Страница X из Y)")
        self.k_sql = QCheckBox("Включать тексты SQL в примечания (пароли скрываются)")
        self.k_cont = QCheckBox("Продолжать, если источник недоступен (блок помечается; иначе отчёт не создаётся)")
        self.g_part = QSpinBox()
        self.g_part.setRange(0, 100000)
        self.g_part.setSpecialValueText("одним файлом")
        self.g_part.setSuffix(" строк")
        self.g_part.setToolTip("Если таблица длиннее, отчёт делится на части (отдельные файлы).")
        self.g_base = QLineEdit()
        self.g_base.setPlaceholderText("необязательно: свой шаблон Word (.docx / .dotx)")
        b_base = QPushButton("Обзор…")
        b_base.clicked.connect(self._pick_base)
        brow = QHBoxLayout()
        brow.addWidget(self.g_base, 1)
        brow.addWidget(b_base)
        for label, w in (("Название шаблона", self.g_name), ("Описание", self.g_desc), ("Стиль", self.g_style),
                         ("Шрифт", self.g_font), ("Размер шрифта", self.g_size), ("Цвет акцента", arow),
                         ("Ориентация", self.g_orient), ("Поля страницы", self.g_margin), ("Язык форматирования", self.g_locale),
                         ("Символ валюты", self.g_currency), ("Заголовок отчёта", self.g_title),
                         ("Подзаголовок", self.g_subtitle), ("Организация", self.g_org), ("Автор", self.g_author),
                         ("Верхний колонтитул", self.g_header), ("Нижний колонтитул", self.g_footer),
                         ("", self.k_titlepage), ("", self.k_toc), ("", self.k_pages), ("", self.k_sql), ("", self.k_cont),
                         ("Делить большие таблицы", self.g_part), ("Шаблон Word", brow)):
            f.addRow(label, w)
            if isinstance(w, QWidget):
                self._watch(w)
        self._watch(self.g_accent)
        self._watch(self.g_base)
        return self._scroll(card)

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self.g_accent.text() or "#1F4E79"), self, "Цвет акцента")
        if c.isValid():
            self.g_accent.setText(c.name().upper())

    def _pick_base(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Шаблон Word", "", "Word (*.docx *.dotx);;Все файлы (*)")
        if path:
            self.g_base.setText(path)

    # ---- вкладка «Параметры» ---------------------------------------------
    def _tab_params(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        info = QLabel("Параметры запрашиваются у пользователя при формировании отчёта. В SQL используйте :имя "
                      "(значение передаётся параметром, а не подставляется в текст запроса), в тексте — {{param.имя}}.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        lay.addWidget(info)
        self.e_params = TableEditor([Col("name", "Имя (латиницей)", width=140), Col("label", "Подпись", width=180),
                                     Col("type", "Тип", "combo", PARAM_TYPES_RU, 130, "text"),
                                     Col("default", "По умолчанию", width=140), Col("required", "Обязательный", "check", default=False)],
                                    movable=True, min_height=220)
        self._watch(self.e_params)
        lay.addWidget(self.e_params, 1)
        return w

    # ---- вкладка «Источники данных» ----------------------------------------
    def _tab_sources(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.src_list = QListWidget()
        self.src_list.setMaximumWidth(210)
        self.src_list.currentRowChanged.connect(self._on_source_row)
        left.addWidget(self.src_list, 1)
        for text, fn in (("＋ Добавить", self.add_source), ("Из избранного…", self.add_source_from_favorite),
                         ("Удалить", self.remove_source)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            left.addWidget(b)
        lay.addLayout(left)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        top = QFormLayout()
        self.s_id = QLineEdit()
        self.s_id.setToolTip("Идентификатор для ссылок из блоков и переменных ({{rows.ИД}})")
        self.s_name = QLineEdit()
        self.s_kind = _combo(KINDS_RU)
        self.s_kind.currentIndexChanged.connect(self._on_kind)
        self.s_db = QLineEdit()
        self.s_db.setPlaceholderText("необязательно: база данных (USE) или база таблицы")
        self.s_limit = QSpinBox()
        self.s_limit.setRange(0, 100000)
        self.s_limit.setSpecialValueText("по общему лимиту")
        for label, wd in (("Идентификатор", self.s_id), ("Название", self.s_name), ("Тип источника", self.s_kind),
                          ("База данных", self.s_db), ("Лимит строк источника", self.s_limit)):
            top.addRow(label, wd)
            self._watch(wd)
        cl.addLayout(top)

        self.s_stack = QStackedWidget()
        self.s_sql = CodeEditor(self.ctx.theme, self.svc.settings.get("editor_font_size"))
        self.s_sql.setMinimumHeight(170)
        self.s_sql.setPlaceholderText("SELECT … WHERE дата >= :date_from  — только чтение, один пакет")
        self._watch(self.s_sql)
        self.s_stack.addWidget(self.s_sql)

        tbl = QWidget()
        tf = QFormLayout(tbl)
        self.s_schema = QLineEdit()
        self.s_table = QLineEdit()
        self.s_cols = QLineEdit()
        self.s_cols.setPlaceholderText("через запятую; пусто — все столбцы")
        self.s_order = QLineEdit()
        self.s_order.setPlaceholderText("например: Сумма desc, Дата")
        self.s_group = QLineEdit()
        self.s_group.setPlaceholderText("столбцы группировки через запятую")
        self.s_filters = TableEditor([Col("column", "Столбец", width=150), Col("op", "Оператор", "combo", OPS_RU, 160, "="),
                                      Col("value", "Значение или :параметр", width=200)], min_height=100)
        self.s_aggs = TableEditor([Col("column", "Столбец (* для COUNT)", width=170, default="*"),
                                   Col("func", "Функция", "combo", AGG_RU, 140, "sum"), Col("alias", "Название в отчёте", width=170)],
                                  min_height=90)
        for label, wd in (("Схема", self.s_schema), ("Таблица / представление", self.s_table), ("Столбцы", self.s_cols),
                          ("Фильтры (AND)", self.s_filters), ("Группировка", self.s_group), ("Агрегаты", self.s_aggs),
                          ("Сортировка", self.s_order)):
            tf.addRow(label, wd)
            self._watch(wd)
        self.s_stack.addWidget(self._scroll(tbl))

        proc = QWidget()
        pf = QFormLayout(proc)
        self.s_proc = QLineEdit()
        self.s_proc.setPlaceholderText("схема.имя_процедуры")
        self.s_pparams = TableEditor([Col("name", "Параметр (без @)", width=170), Col("value", "Значение или :параметр", width=220)],
                                     min_height=90)
        self.s_trust = QCheckBox("Я подтверждаю: процедура только читает данные и не изменяет их")
        warn = QLabel("Отчёты выполняют только чтение, но тело процедуры программа проверить не может — ответственность "
                      "за безопасность процедуры лежит на вас. Лучше использовать учётную запись только на чтение.")
        warn.setWordWrap(True)
        warn.setObjectName("Muted")
        for label, wd in (("Процедура", self.s_proc), ("Параметры", self.s_pparams), ("", self.s_trust), ("", warn)):
            pf.addRow(label, wd)
            if isinstance(wd, QWidget) and wd is not warn:
                self._watch(wd)
        self.s_stack.addWidget(proc)
        cl.addWidget(self.s_stack, 1)

        row = QHBoxLayout()
        b1 = QPushButton("Показать SQL")
        b1.clicked.connect(self.show_sql)
        b2 = QPushButton("Проверить (первые 20 строк)")
        b2.clicked.connect(self.test_source)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        cl.addLayout(row)
        self.s_preview = QPlainTextEdit()
        self.s_preview.setReadOnly(True)
        self.s_preview.setMaximumHeight(90)
        self.s_preview.setPlaceholderText("Здесь будет показан итоговый SQL с параметрами")
        cl.addWidget(self.s_preview)
        lay.addWidget(card, 1)
        return w

    # ---- вкладка «Блоки» -----------------------------------------------------
    def _tab_blocks(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.blk_list = QListWidget()
        self.blk_list.setMaximumWidth(250)
        self.blk_list.currentRowChanged.connect(self._on_block_row)
        left.addWidget(self.blk_list, 1)
        row = QHBoxLayout()
        b_add = QPushButton("＋ Блок")
        menu = QMenu(b_add)
        for t in BLOCK_TYPES:
            menu.addAction(BLOCK_TITLES[t], lambda _=False, tt=t: self.add_block(tt))
        b_add.setMenu(menu)
        row.addWidget(b_add)
        for text, fn in (("−", self.remove_block), ("↑", lambda: self.move_block(-1)), ("↓", lambda: self.move_block(1))):
            b = QPushButton(text)
            b.setFixedWidth(34)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        left.addLayout(row)
        lay.addLayout(left)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        top = QFormLayout()
        self.b_type = QLabel("")
        self.b_title = QLineEdit()
        self.b_level = QSpinBox()
        self.b_level.setRange(1, 3)
        self.b_source = QComboBox()
        self.b_text = QPlainTextEdit()
        self.b_text.setMinimumHeight(110)
        self.b_text.setPlaceholderText("Текст блока. Переменные: {{date}}, {{database}}, {{param.имя}}, {{rows.источник}} …")
        for label, wd in (("Тип блока", self.b_type), ("Заголовок", self.b_title), ("Уровень заголовка", self.b_level),
                          ("Источник данных", self.b_source), ("Текст", self.b_text)):
            top.addRow(label, wd)
            if wd is not self.b_type:
                self._watch(wd)
        self._top_rows = top
        cl.addLayout(top)

        self.grp_table = QGroupBox("Таблица")
        gt = QFormLayout(self.grp_table)
        self.b_max = QSpinBox()
        self.b_max.setRange(0, 100000)
        self.b_max.setSpecialValueText("общий лимит")
        self.b_group = QLineEdit()
        self.b_group.setPlaceholderText("столбец: отдельный раздел для каждого значения")
        self.b_rownum = QCheckBox("Нумеровать строки")
        self.b_sort = QLineEdit()
        self.b_sort.setPlaceholderText("например: Сумма desc, Дата")
        self.b_cols = TableEditor([Col("name", "Столбец", width=130), Col("title", "Заголовок", width=130),
                                   Col("hidden", "Скрыть", "check", default=False),
                                   Col("format", "Формат", "combo", FORMATS_RU, 110, "auto"),
                                   Col("decimals", "Знаков", "int", width=60, default=-1),
                                   Col("total", "Итог", "combo", TOTALS_RU, 100, "none"),
                                   Col("align", "Выравн.", "combo", ALIGN_RU, 90, "auto")], movable=True, min_height=140)
        b_fill = QPushButton("Заполнить столбцы из проверенного источника")
        b_fill.clicked.connect(self.fill_columns)
        self.b_comp = TableEditor([Col("title", "Название", width=150), Col("op", "Операция", "combo", COMPUTED_RU, 130, "div"),
                                   Col("a", "A (столбец или число)", width=150), Col("b", "B (столбец или число)", width=150),
                                   Col("decimals", "Знаков", "int", width=60, default=2)], min_height=90)
        for label, wd in (("Макс. строк", self.b_max), ("Разделы по столбцу", self.b_group), ("", self.b_rownum),
                          ("Сортировка", self.b_sort), ("Столбцы (пусто — все)", self.b_cols), ("", b_fill),
                          ("Вычисляемые столбцы", self.b_comp)):
            gt.addRow(label, wd)
            if isinstance(wd, QWidget) and wd is not b_fill:
                self._watch(wd)
        self.grp_stats = QGroupBox("Статистика")
        gs = QFormLayout(self.grp_stats)
        self.b_statcols = QLineEdit()
        self.b_statcols.setPlaceholderText("столбцы через запятую; пусто — все подходящие")
        self.b_metrics = TableEditor([Col("title", "Показатель", width=170), Col("op", "Операция", "combo", METRICS_RU, 170, "sum"),
                                      Col("column", "Столбец A", width=130), Col("column2", "Столбец B (для отношения)", width=170)],
                                     min_height=100)
        gs.addRow("Столбцы", self.b_statcols)
        gs.addRow("Свои показатели", self.b_metrics)
        self._watch(self.b_statcols)
        self._watch(self.b_metrics)
        self.grp_chart = QGroupBox("Диаграмма")
        gc = QFormLayout(self.grp_chart)
        self.b_kind = _combo(CHART_KINDS_RU)
        self.b_x = QLineEdit()
        self.b_x.setPlaceholderText("категория или дата")
        self.b_y = QLineEdit()
        self.b_y.setPlaceholderText("числовые столбцы через запятую (для «количество строк» не нужен)")
        self.b_agg = _combo(CHART_AGGS_RU)
        self.b_topn = QSpinBox()
        self.b_topn.setRange(2, 50)
        for label, wd in (("Вид", self.b_kind), ("Ось X (категории)", self.b_x), ("Значения (Y)", self.b_y),
                          ("Агрегация", self.b_agg), ("Показывать топ", self.b_topn)):
            gc.addRow(label, wd)
            self._watch(wd)
        self.grp_query = QGroupBox("Код запроса и результат")
        gq = QFormLayout(self.grp_query)
        self.b_qcode = QCheckBox("Показывать текст запроса и значения параметров")
        self.b_qres = QCheckBox("Показывать результат выполнения (таблица строк)")
        self.b_qrows = QSpinBox()
        self.b_qrows.setRange(0, 100000)
        self.b_qrows.setSpecialValueText("общий лимит")
        gq.addRow("", self.b_qcode)
        gq.addRow("", self.b_qres)
        gq.addRow("Строк результата не больше", self.b_qrows)
        for wd in (self.b_qcode, self.b_qres, self.b_qrows):
            self._watch(wd)
        for g in (self.grp_table, self.grp_query, self.grp_stats, self.grp_chart):
            cl.addWidget(g)
        cl.addStretch(1)
        lay.addWidget(self._scroll(card), 1)
        return w

    # ============================================================ списки
    def _reload_connections(self) -> None:
        cur = self.c_conn.currentData()
        self.c_conn.clear()
        for p in self.svc.profiles.repo.list():
            if self.svc.manager.primary(p.id) is not None:
                self.c_conn.addItem(p.name, p.id)
        self.c_conn.setCurrentIndex(max(0, self.c_conn.findData(cur)))

    def _reload_list(self, select_id: str | None = None) -> None:
        self._infos = self.store.list()
        self._loading = True
        self.list.clear()
        for i in self._infos:
            self.list.addItem(i.name + ("   · встроенный" if i.builtin else ""))
        self._loading = False
        if select_id:
            for n, i in enumerate(self._infos):
                if i.id == select_id:
                    self._loading = True
                    self.list.setCurrentRow(n)
                    self._loading = False
        if self.store.errors:
            self.ctx.message.emit(f"Не удалось прочитать шаблонов: {len(self.store.errors)} (см. журнал)")
            for e in self.store.errors:
                log.warning("Шаблон пропущен: %s", e)

    def _on_list_row(self, row: int) -> None:
        if self._loading or not (0 <= row < len(self._infos)):
            return
        if not self._confirm_discard():
            self._reload_list(self.t.id)
            return
        try:
            self.load(self.store.load(self._infos[row].id))
        except TemplateError as exc:
            QMessageBox.warning(self, "Шаблон", str(exc))

    def select_template(self, template_id: str) -> None:
        if not self._confirm_discard():
            return
        try:
            self.load(self.store.load(template_id))
        except TemplateError as exc:
            QMessageBox.warning(self, "Шаблон", str(exc))
            return
        self._reload_list(template_id)

    @staticmethod
    def _new_template() -> ReportTemplate:
        t = ReportTemplate(id=new_id("t"), name="Новый отчёт")
        t.sources = [DataSource(id="s1", name="Данные", kind="sql", sql="SELECT TOP (100) name AS [Имя], type_desc AS [Тип], "
                                                                  "create_date AS [Создан]\nFROM sys.objects\nWHERE is_ms_shipped = 0\nORDER BY create_date DESC")]
        t.blocks = [Block(id=new_id("b"), type="table", title="Данные", source="s1", level=1),
                    Block(id=new_id("b"), type="notes", title="Примечания", level=1)]
        return t

    # ======================================================= загрузка / сбор
    def load(self, t: ReportTemplate) -> None:
        self._loading = True
        self.t = t
        self.g_name.setText(t.name)
        self.g_desc.setText(t.description)
        self.g_style.setCurrentIndex(max(0, self.g_style.findData(t.style)))
        self.g_font.setCurrentIndex(max(0, self.g_font.findData(t.font_name)))
        if t.font_name and self.g_font.findData(t.font_name) < 0:
            self.g_font.setEditText(t.font_name)
        self.g_size.setValue(int(t.font_size))
        self.g_accent.setText(t.accent)
        self.g_orient.setCurrentIndex(max(0, self.g_orient.findData(t.orientation)))
        self.g_margin.setValue(int(round(t.margin_cm)))
        self.g_locale.setCurrentIndex(max(0, self.g_locale.findData(t.locale)))
        self.g_currency.setText(t.currency_symbol)
        self.g_title.setText(t.title)
        self.g_subtitle.setText(t.subtitle)
        self.g_org.setText(t.organization)
        self.g_author.setText(t.author)
        self.g_header.setText(t.header_text)
        self.g_footer.setText(t.footer_text)
        self.k_titlepage.setChecked(t.title_page)
        self.k_toc.setChecked(t.toc)
        self.k_pages.setChecked(t.page_numbers)
        self.k_sql.setChecked(t.include_sql)
        self.k_cont.setChecked(t.continue_on_error)
        self.g_part.setValue(t.part_rows)
        self.g_base.setText(t.base_docx)
        self.e_params.set_rows([p.__dict__ for p in t.params])
        self.sources = [DataSource.from_dict(s.__dict__ | {"filters": [f.__dict__ for f in s.filters],
                                                           "order_by": [o.__dict__ for o in s.order_by],
                                                           "aggregates": [a.__dict__ for a in s.aggregates]})
                        for s in t.sources]
        self.blocks = [Block.from_dict(self._block_dict(b)) for b in t.blocks]
        self._src_idx = self._blk_idx = -1
        self._refresh_sources()
        self._refresh_blocks()
        self._select_source(0 if self.sources else -1)
        self._select_block(0 if self.blocks else -1)
        self._loading = False
        self._dirty = False
        self.dirty_label.setText("")

    @staticmethod
    def _block_dict(b: Block) -> dict:
        d = dict(b.__dict__)
        for k in ("columns", "sort", "computed", "metrics"):
            d[k] = [x.__dict__ for x in getattr(b, k)]
        d["y"] = list(b.y)
        d["stat_columns"] = list(b.stat_columns)
        return d

    def collect(self) -> ReportTemplate:
        self._commit_source()
        self._commit_block()
        t = ReportTemplate.from_dict(self.t.to_dict())
        t.name = self.g_name.text().strip()
        t.description = self.g_desc.text().strip()
        t.style = self.g_style.currentData()
        t.font_name = (self.g_font.currentData() if self.g_font.currentText() == self.g_font.itemText(self.g_font.currentIndex())
                       else self.g_font.currentText().strip()) or ""
        t.font_size = float(self.g_size.value())
        t.accent = self.g_accent.text().strip()
        t.orientation = self.g_orient.currentData()
        t.margin_cm = float(self.g_margin.value())
        t.locale = self.g_locale.currentData()
        t.currency_symbol = self.g_currency.text().strip()
        t.title, t.subtitle = self.g_title.text(), self.g_subtitle.text()
        t.organization, t.author = self.g_org.text(), self.g_author.text()
        t.header_text, t.footer_text = self.g_header.text(), self.g_footer.text()
        t.title_page, t.toc, t.page_numbers = self.k_titlepage.isChecked(), self.k_toc.isChecked(), self.k_pages.isChecked()
        t.include_sql, t.continue_on_error = self.k_sql.isChecked(), self.k_cont.isChecked()
        t.part_rows = self.g_part.value()
        t.base_docx = self.g_base.text().strip()
        t.params = [Param(**r) for r in self.e_params.rows()]
        t.sources = [DataSource.from_dict(self._src_dict(s)) for s in self.sources]
        t.blocks = [Block.from_dict(self._block_dict(b)) for b in self.blocks]
        return t

    @staticmethod
    def _src_dict(s: DataSource) -> dict:
        d = dict(s.__dict__)
        d["filters"] = [f.__dict__ for f in s.filters]
        d["order_by"] = [o.__dict__ for o in s.order_by]
        d["aggregates"] = [a.__dict__ for a in s.aggregates]
        return d

    # ============================================================ источники
    def _refresh_sources(self) -> None:
        was = self._loading
        self._loading = True
        self.src_list.clear()
        for s in self.sources:
            self.src_list.addItem(f"{s.name or s.id}  [{s.id}]")
        self._loading = was
        self._refresh_block_sources()

    def _on_source_row(self, row: int) -> None:
        if self._loading:
            return
        self._commit_source()
        self._select_source(row)

    def _select_source(self, i: int) -> None:
        self._src_idx = i if 0 <= i < len(self.sources) else -1
        was = self._loading
        self._loading = True
        if self._src_idx >= 0:
            self.src_list.setCurrentRow(self._src_idx)
            s = self.sources[self._src_idx]
            self.s_id.setText(s.id)
            self.s_name.setText(s.name)
            self.s_kind.setCurrentIndex(max(0, self.s_kind.findData(s.kind)))
            self.s_db.setText(s.database)
            self.s_limit.setValue(s.row_limit)
            self.s_sql.setPlainText(s.sql)
            self.s_schema.setText(s.schema)
            self.s_table.setText(s.table)
            self.s_cols.setText(", ".join(s.columns))
            self.s_order.setText(format_sort(s.order_by))
            self.s_group.setText(", ".join(s.group_by))
            self.s_filters.set_rows([f.__dict__ for f in s.filters])
            self.s_aggs.set_rows([a.__dict__ for a in s.aggregates])
            self.s_proc.setText(s.procedure)
            self.s_pparams.set_rows([{"name": k, "value": v} for k, v in s.proc_params.items()])
            self.s_trust.setChecked(s.proc_trusted)
            self.s_stack.setCurrentIndex({"sql": 0, "table": 1, "procedure": 2}.get(s.kind, 0))
        self._loading = was

    def _commit_source(self) -> None:
        if not (0 <= self._src_idx < len(self.sources)):
            return
        s = self.sources[self._src_idx]
        old_id = s.id
        s.id = self.s_id.text().strip() or old_id
        s.name = self.s_name.text().strip()
        s.kind = self.s_kind.currentData()
        s.database = self.s_db.text().strip()
        s.row_limit = self.s_limit.value()
        s.sql = self.s_sql.toPlainText()
        s.schema = self.s_schema.text().strip() or "dbo"
        s.table = self.s_table.text().strip()
        s.columns = split_list(self.s_cols.text())
        s.order_by = parse_sort(self.s_order.text())
        s.group_by = split_list(self.s_group.text())
        s.filters = [Filter(**r) for r in self.s_filters.rows()]
        s.aggregates = [Aggregate(**r) for r in self.s_aggs.rows()]
        s.procedure = self.s_proc.text().strip()
        s.proc_params = {r["name"]: r["value"] for r in self.s_pparams.rows()}
        s.proc_trusted = self.s_trust.isChecked()
        if s.id != old_id:
            for b in self.blocks:
                if b.source == old_id:
                    b.source = s.id
            self._columns[s.id] = self._columns.pop(old_id, [])

    def _on_kind(self) -> None:
        self.s_stack.setCurrentIndex({"sql": 0, "table": 1, "procedure": 2}.get(self.s_kind.currentData(), 0))

    def add_source(self) -> None:
        self._commit_source()
        n = 1
        ids = {s.id for s in self.sources}
        while f"s{n}" in ids:
            n += 1
        self.sources.append(DataSource(id=f"s{n}", name=f"Источник {n}", kind="sql", sql="SELECT 1 AS [Значение]"))
        self._refresh_sources()
        self._select_source(len(self.sources) - 1)
        self._mark_dirty()

    def add_source_from_favorite(self) -> None:
        """Источник из сохранённого запроса («Избранное» SQL-редактора); его параметры :имя добавляются в шаблон."""
        favorites = self.svc.favorites.list()
        if not favorites:
            QMessageBox.information(self, "Избранное", "Сохранённых запросов нет. Сохраните запрос в SQL-редакторе "
                                                       "(кнопка «В избранное»).")
            return
        titles = [f.title for f in favorites]
        title, ok = QInputDialog.getItem(self, "Сохранённый запрос", "Какой запрос использовать как источник:", titles, 0, False)
        if not ok or title not in titles:
            return
        fav = favorites[titles.index(title)]
        self._commit_source()
        ids = {s.id for s in self.sources}
        n = 1
        while f"s{n}" in ids:
            n += 1
        self.sources.append(DataSource(id=f"s{n}", name=fav.title, kind="sql", sql=fav.sql))
        have = {r["name"] for r in self.e_params.rows()}
        from reports.variables import find_named_params
        for name in find_named_params(fav.sql):
            if name not in have:
                self.e_params.add_row({"name": name, "label": name, "type": "text", "default": "", "required": True})
        self._refresh_sources()
        self._select_source(len(self.sources) - 1)
        self._mark_dirty()
        self.ctx.message.emit(f"Источник «{fav.title}» добавлен. Чтобы вывести код и результат, добавьте блок "
                              "«Код запроса и результат».")

    def remove_source(self) -> None:
        if not (0 <= self._src_idx < len(self.sources)):
            return
        s = self.sources[self._src_idx]
        used = [b for b in self.blocks if b.source == s.id]
        if used and QMessageBox.question(
                self, "Удаление источника", f"Источник используется блоками ({len(used)}). Удалить и очистить ссылки?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) \
                != QMessageBox.StandardButton.Yes:
            return
        for b in used:
            b.source = ""
        del self.sources[self._src_idx]
        self._src_idx = -1
        self._refresh_sources()
        self._select_source(min(self._src_idx if self._src_idx >= 0 else 0, len(self.sources) - 1))
        self._mark_dirty()

    def _dummy_values(self) -> dict:
        return {r["name"]: "<значение>" for r in self.e_params.rows()}

    def show_sql(self) -> None:
        self._commit_source()
        if not (0 <= self._src_idx < len(self.sources)):
            return
        try:
            sql, vals = build_query(self.sources[self._src_idx], self._dummy_values(), 100)
        except Exception as exc:  # noqa: BLE001
            self.s_preview.setPlainText(f"Ошибка: {exc}")
            return
        self.s_preview.setPlainText(sql + (f"\n-- параметры (?): {vals!r}" if vals else ""))

    def test_source(self) -> None:
        self._commit_source()
        if not (0 <= self._src_idx < len(self.sources)):
            return
        pid = self.c_conn.currentData()
        if pid is None:
            QMessageBox.information(self, "Проверка источника", "Подключитесь к серверу и выберите подключение сверху.")
            return
        src = DataSource.from_dict(self._src_dict(self.sources[self._src_idx]))
        try:
            values = coerce_params([Param(**r) for r in self.e_params.rows()], {})
        except ParamError as exc:
            QMessageBox.information(self, "Проверка источника",
                                    f"{exc}\nУкажите значение по умолчанию у параметра, чтобы проверить запрос.")
            return
        mgr = self.svc.manager
        batch = self.svc.settings.get("fetch_batch_size")

        def job(c):
            with mgr.lease_session(pid, "preview") as conn:
                return fetch_dataset(conn, src, values, max_rows=20, batch_size=batch, cancel_token=c.cancel_token)

        def done(ds):
            self._columns[src.id] = list(ds.columns)
            note = ("Показаны первые 20 строк. " if ds.truncated else "") + f"Столбцов: {len(ds.columns)}."
            PreviewDialog(self, f"Проверка: {src.name or src.id}", ds.columns, ds.rows, note).exec()

        try:
            self.ctx.runner.submit("Проверка источника отчёта", job, on_success=done,
                                   on_error=lambda m: QMessageBox.warning(self, "Проверка источника", m),
                                   key="template-source-test")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Проверка источника", str(exc))

    # =============================================================== блоки
    def _block_label(self, b: Block) -> str:
        label = BLOCK_TITLES.get(b.type, b.type)
        if b.title:
            label += f": {b.title}"
        elif b.type in BLOCKS_WITH_SOURCE and b.source:
            label += f" ({b.source})"
        return label

    def _refresh_blocks(self) -> None:
        was = self._loading
        self._loading = True
        self.blk_list.clear()
        for b in self.blocks:
            self.blk_list.addItem(self._block_label(b))
        self._loading = was

    def _refresh_block_sources(self) -> None:
        cur = self.b_source.currentData()
        was = self._loading
        self._loading = True
        self.b_source.clear()
        self.b_source.addItem("(не выбран)", "")
        for s in self.sources:
            self.b_source.addItem(f"{s.name or s.id} [{s.id}]", s.id)
        self.b_source.setCurrentIndex(max(0, self.b_source.findData(cur)))
        self._loading = was

    def _on_block_row(self, row: int) -> None:
        if self._loading:
            return
        self._commit_block()
        self._select_block(row)

    def _select_block(self, i: int) -> None:
        self._blk_idx = i if 0 <= i < len(self.blocks) else -1
        was = self._loading
        self._loading = True
        self._refresh_block_sources()
        if self._blk_idx >= 0:
            self.blk_list.setCurrentRow(self._blk_idx)
            b = self.blocks[self._blk_idx]
            self.b_type.setText(BLOCK_TITLES.get(b.type, b.type))
            self.b_title.setText(b.title)
            self.b_level.setValue(max(1, min(3, b.level)))
            self.b_source.setCurrentIndex(max(0, self.b_source.findData(b.source)))
            self.b_text.setPlainText(b.text)
            self.b_max.setValue(b.max_rows)
            self.b_qrows.setValue(b.max_rows)
            self.b_qcode.setChecked(b.show_code)
            self.b_qres.setChecked(b.show_result)
            self.b_group.setText(b.group_by)
            self.b_rownum.setChecked(b.row_numbers)
            self.b_sort.setText(format_sort(b.sort))
            self.b_cols.set_rows([c.__dict__ for c in b.columns])
            self.b_comp.set_rows([c.__dict__ for c in b.computed])
            self.b_statcols.setText(", ".join(b.stat_columns))
            self.b_metrics.set_rows([m.__dict__ for m in b.metrics])
            self.b_kind.setCurrentIndex(max(0, self.b_kind.findData(b.chart_kind)))
            self.b_x.setText(b.x)
            self.b_y.setText(", ".join(b.y))
            self.b_agg.setCurrentIndex(max(0, self.b_agg.findData(b.agg)))
            self.b_topn.setValue(b.top_n)
            self._apply_block_visibility(b.type)
        self._loading = was

    def _apply_block_visibility(self, t: str) -> None:
        self.b_title.setEnabled(t != "page_break")
        self.b_level.setEnabled(t in ("heading", "table", "query", "stats", "chart", "summary", "notes"))
        self.b_source.setEnabled(t in BLOCKS_WITH_SOURCE)
        self.b_text.setEnabled(t == "text")
        self.grp_table.setVisible(t == "table")
        self.grp_query.setVisible(t == "query")
        self.grp_stats.setVisible(t == "stats")
        self.grp_chart.setVisible(t == "chart")

    def _commit_block(self) -> None:
        if not (0 <= self._blk_idx < len(self.blocks)):
            return
        b = self.blocks[self._blk_idx]
        b.title = self.b_title.text()
        b.level = self.b_level.value()
        b.source = self.b_source.currentData() or ""
        b.text = self.b_text.toPlainText()
        b.max_rows = self.b_qrows.value() if b.type == "query" else self.b_max.value()
        b.show_code = self.b_qcode.isChecked()
        b.show_result = self.b_qres.isChecked()
        b.group_by = self.b_group.text().strip()
        b.row_numbers = self.b_rownum.isChecked()
        b.sort = parse_sort(self.b_sort.text())
        b.columns = [ColumnSpec(**r) for r in self.b_cols.rows()]
        b.computed = [ComputedColumn(**r) for r in self.b_comp.rows()]
        b.stat_columns = split_list(self.b_statcols.text())
        b.metrics = [Metric(**r) for r in self.b_metrics.rows()]
        b.chart_kind = self.b_kind.currentData()
        b.x = self.b_x.text().strip()
        b.y = split_list(self.b_y.text())
        b.agg = self.b_agg.currentData()
        b.top_n = self.b_topn.value()
        item = self.blk_list.item(self._blk_idx)
        if item is not None:
            item.setText(self._block_label(b))

    def add_block(self, btype: str) -> None:
        self._commit_block()
        src = self.sources[0].id if self.sources and btype in BLOCKS_WITH_SOURCE else ""
        title = {"heading": "Раздел", "notes": "Примечания", "summary": "Выводы", "query": "Запрос и результат"}.get(btype, "")
        blk = Block(id=new_id("b"), type=btype, title=title, source=src, level=1)
        pos = self._blk_idx + 1 if self._blk_idx >= 0 else len(self.blocks)
        self.blocks.insert(pos, blk)
        self._refresh_blocks()
        self._select_block(pos)
        self._mark_dirty()

    def remove_block(self) -> None:
        if 0 <= self._blk_idx < len(self.blocks):
            del self.blocks[self._blk_idx]
            idx = min(self._blk_idx, len(self.blocks) - 1)
            self._blk_idx = -1
            self._refresh_blocks()
            self._select_block(idx)
            self._mark_dirty()

    def move_block(self, delta: int) -> None:
        self._commit_block()
        i, n = self._blk_idx, self._blk_idx + delta
        if 0 <= i < len(self.blocks) and 0 <= n < len(self.blocks):
            self.blocks[i], self.blocks[n] = self.blocks[n], self.blocks[i]
            self._refresh_blocks()
            self._select_block(n)
            self._mark_dirty()

    def fill_columns(self) -> None:
        self._commit_block()
        if not (0 <= self._blk_idx < len(self.blocks)):
            return
        cols = self._columns.get(self.blocks[self._blk_idx].source)
        if not cols:
            QMessageBox.information(self, "Столбцы", "Сначала проверьте источник на вкладке «Источники данных» "
                                                     "(кнопка «Проверить») — так программа узнает его столбцы.")
            return
        self.b_cols.set_rows([{"name": c} for c in cols])
        self._mark_dirty()

    # ===================================================== действия над шаблонами
    def check(self) -> None:
        errs = validate_template(self.collect())
        if errs:
            QMessageBox.warning(self, "Проверка шаблона", "Найдены проблемы:\n\n• " + "\n• ".join(errs))
        else:
            QMessageBox.information(self, "Проверка шаблона", "Ошибок не найдено: шаблон готов к использованию.")

    def save(self) -> bool:
        t = self.collect()
        errs = validate_template(t)
        if errs:
            a = QMessageBox.question(
                self, "Шаблон содержит ошибки", "Шаблон можно сохранить как черновик, но сформировать по нему отчёт "
                "не получится, пока ошибки не исправлены:\n\n• " + "\n• ".join(errs[:8]) + "\n\nСохранить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if a != QMessageBox.StandardButton.Yes:
                return False
        if self.store.is_builtin(t.id):
            t.id = new_id("t")
            if not t.name.endswith("(копия)"):
                t.name += " (копия)"
            self.ctx.message.emit("Встроенный шаблон не изменяется — сохранена копия.")
        try:
            self.store.save(t)
        except (TemplateError, OSError) as exc:
            QMessageBox.warning(self, "Не удалось сохранить", str(exc))
            return False
        self.t = t
        self._dirty = False
        self.dirty_label.setText("")
        self._reload_list(t.id)
        self.ctx.message.emit(f"Шаблон «{t.name}» сохранён")
        self.ctx.reports_changed.emit()
        return True

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        box = QMessageBox(QMessageBox.Icon.Question, "Несохранённые изменения", "Сохранить изменения шаблона?", parent=self)
        save = box.addButton("Сохранить", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Не сохранять", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is save:
            return self.save()
        return box.clickedButton() is discard

    def prepare_close(self) -> bool:
        return self._confirm_discard()

    def new_template(self) -> None:
        if self._confirm_discard():
            self.load(self._new_template())
            self._dirty = True
            self.dirty_label.setText("● новый, ещё не сохранён")

    def duplicate(self) -> None:
        if not self._confirm_discard():
            return
        try:
            copy = self.store.duplicate(self.t.id) if self._saved(self.t.id) else None
        except TemplateError as exc:
            QMessageBox.warning(self, "Копия", str(exc))
            return
        if copy is None:
            QMessageBox.information(self, "Копия", "Сначала сохраните шаблон.")
            return
        self._reload_list(copy.id)
        self.load(copy)

    def _saved(self, tid: str) -> bool:
        return any(i.id == tid for i in self.store.list())

    def delete(self) -> None:
        tid = self.t.id
        if self.store.is_builtin(tid):
            QMessageBox.information(self, "Удаление", "Встроенный шаблон удалить нельзя.")
            return
        if not self._saved(tid):
            self.load(self._new_template())
            return
        a = QMessageBox.question(self, "Удаление шаблона", f"Удалить шаблон «{self.t.name}»?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                 QMessageBox.StandardButton.No)
        if a != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(tid)
        except TemplateError as exc:
            QMessageBox.warning(self, "Удаление", str(exc))
            return
        self._dirty = False
        self._reload_list()
        self.load(self._new_template())
        self.ctx.reports_changed.emit()

    def import_file(self) -> None:
        from pathlib import Path
        path, _ = QFileDialog.getOpenFileName(self, "Импорт шаблона", "", "Шаблон отчёта (*.json)")
        if not path:
            return
        try:
            t = self.store.import_file(Path(path))
        except TemplateError as exc:
            QMessageBox.warning(self, "Импорт", str(exc))
            return
        self._reload_list(t.id)
        self.load(t)
        self.ctx.reports_changed.emit()

    def export_file(self) -> None:
        from pathlib import Path
        if not self._saved(self.t.id):
            QMessageBox.information(self, "Экспорт", "Сначала сохраните шаблон.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт шаблона", f"{self.t.name}.json", "Шаблон отчёта (*.json)")
        if path:
            try:
                self.store.export_file(self.t.id, Path(path))
                self.ctx.message.emit(f"Шаблон экспортирован: {path}")
            except (TemplateError, OSError) as exc:
                QMessageBox.warning(self, "Экспорт", str(exc))
