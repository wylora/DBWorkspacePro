"""Редактор T-SQL: подсветка, номера строк, автоотступ, подчёркивание ошибок, автодополнение."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRect, QRectF, QSize, QStringListModel, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QKeyEvent, QPainter, QSyntaxHighlighter, QTextCharFormat,
                           QTextCursor, QTextFormat, QTextOption)
from PySide6.QtWidgets import QCompleter, QPlainTextEdit, QTextEdit, QWidget

from database.completion import KIND_LABELS, Completion
from database.sql_keywords import DATATYPES, FUNCTIONS, KEYWORDS
from database.sql_lexer import NORMAL, lex_line

SYNTAX_COLORS = {
    "light": dict(keyword="#0000CC", function="#B000B0", type="#007A7A", string="#B03030", comment="#007A00",
                  number="#9C5A00", var="#7A3E9D", ident="#444444", line="#EEF3FF", gutter_bg="#F0F2F7",
                  gutter_fg="#8A93A6", error="#D32F2F"),
    "dark": dict(keyword="#6CB6FF", function="#D2A8FF", type="#56D4DD", string="#FF9B8F", comment="#7EE787",
                 number="#FFB86B", var="#C9A0FF", ident="#C8D1DC", line="#243247", gutter_bg="#17202E",
                 gutter_fg="#6B7A90", error="#FF6B6B"),
}


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, document, theme: str = "light"):
        super().__init__(document)
        self._fmt: dict[str, QTextCharFormat] = {}
        self.set_theme(theme, rehighlight=False)

    def set_theme(self, theme: str, rehighlight: bool = True) -> None:
        c = SYNTAX_COLORS.get(theme, SYNTAX_COLORS["light"])
        self._fmt = {}
        for kind in ("keyword", "function", "type", "string", "comment", "number", "var", "ident"):
            f = QTextCharFormat()
            f.setForeground(QColor(c[kind]))
            if kind == "keyword":
                f.setFontWeight(QFont.Weight.Bold)
            if kind == "comment":
                f.setFontItalic(True)
            self._fmt[kind] = f
        if rehighlight:
            self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        prev = self.previousBlockState()
        tokens, state = lex_line(text, NORMAL if prev < 0 else prev)
        for t in tokens:
            kind = t.kind
            if kind == "word":
                u = t.upper
                kind = "keyword" if u in KEYWORDS else "function" if u in FUNCTIONS else \
                    "type" if u in DATATYPES else ""
            if kind in self._fmt:
                self.setFormat(t.start, t.end - t.start, self._fmt[kind])
        self.setCurrentBlockState(state)


class _LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        self._editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    """completion_provider(text, cursor_pos) -> (начало_префикса, [Completion]) — задаётся владельцем."""

    def __init__(self, theme: str = "light", font_size: int = 11, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._errors: list[tuple[int, int, str]] = []
        self.completion_provider: Callable[[str, int], tuple[int, list[Completion]]] | None = None
        self._comp_start = 0
        self._insert_map: dict[str, str] = {}

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(font_size)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.setPlaceholderText("Введите T-SQL. F5 или Ctrl+Enter — выполнить (выделенный фрагмент или весь скрипт).")

        self._highlighter = SqlHighlighter(self.document(), theme)
        self._numbers = _LineNumberArea(self)
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_numbers)
        self.cursorPositionChanged.connect(self._refresh_selections)
        self._update_margin()
        self._refresh_selections()

        self._completer = QCompleter(self)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._model = QStringListModel(self)
        self._completer.setModel(self._model)
        self._completer.activated[str].connect(self._insert_completion)

    # ---- тема и шрифт -------------------------------------------------
    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self._highlighter.set_theme(theme)
        self._refresh_selections()
        self._numbers.update()

    def set_font_size(self, size: int) -> None:
        f = self.font()
        f.setPointSize(size)
        self.setFont(f)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self._update_margin()

    def _c(self) -> dict[str, str]:
        return SYNTAX_COLORS.get(self._theme, SYNTAX_COLORS["light"])

    # ---- номера строк -------------------------------------------------
    def line_number_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self, *_args) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_numbers(self, rect, dy) -> None:
        if dy:
            self._numbers.scroll(0, dy)
        else:
            self._numbers.update(0, rect.y(), self._numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._numbers.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event) -> None:
        c = self._c()
        painter = QPainter(self._numbers)
        painter.fillRect(event.rect(), QColor(c["gutter_bg"]))
        painter.setPen(QColor(c["gutter_fg"]))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(QRectF(0, top, self._numbers.width() - 6, height), str(number + 1),
                                 QTextOption(Qt.AlignmentFlag.AlignRight))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()

    # ---- текущая строка и ошибки --------------------------------------
    def set_errors(self, errors: list[tuple[int, int, str]]) -> None:
        self._errors = list(errors)
        self._refresh_selections()

    def clear_errors(self) -> None:
        if self._errors:
            self.set_errors([])

    def _refresh_selections(self) -> None:
        sels = []
        if not self.isReadOnly():
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(self._c()["line"]))
            fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
            line_cursor = self.textCursor()
            line_cursor.clearSelection()
            sel = QTextEdit.ExtraSelection()
            sel.format = fmt            # присваиваем целиком: PySide отдаёт копии вложенных структур
            sel.cursor = line_cursor
            sels.append(sel)
        length = len(self.toPlainText())
        for start, end, _msg in self._errors:
            if 0 <= start < length:
                efmt = QTextCharFormat()
                efmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
                efmt.setUnderlineColor(QColor(self._c()["error"]))
                cur = QTextCursor(self.document())
                cur.setPosition(start)
                cur.setPosition(min(max(end, start + 1), length), QTextCursor.MoveMode.KeepAnchor)
                sel = QTextEdit.ExtraSelection()
                sel.format = efmt
                sel.cursor = cur
                sels.append(sel)
        self.setExtraSelections(sels)

    def goto_offset(self, offset: int) -> None:
        cur = self.textCursor()
        cur.setPosition(max(0, min(offset, len(self.toPlainText()))))
        self.setTextCursor(cur)
        self.centerCursor()

    # ---- что выполнять -------------------------------------------------
    def sql_to_run(self) -> tuple[str, int]:
        """(текст, смещение в документе): выделенный фрагмент или весь скрипт."""
        cur = self.textCursor()
        if cur.hasSelection():
            return cur.selectedText().replace("\u2029", "\n"), cur.selectionStart()
        return self.toPlainText(), 0

    # ---- ввод: отступы и автодополнение -------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        popup = self._completer.popup()
        key = event.key()
        if popup.isVisible() and key in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Escape,
                                         Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            event.ignore()
            return
        mods = event.modifiers()
        if key == Qt.Key.Key_Space and mods & Qt.KeyboardModifier.ControlModifier:
            self.show_completions(explicit=True)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (mods & Qt.KeyboardModifier.ControlModifier):
            indent = self._current_indent()
            super().keyPressEvent(event)
            self.insertPlainText(indent)
            return
        if key == Qt.Key.Key_Tab and not (mods & Qt.KeyboardModifier.ControlModifier):
            if self.textCursor().hasSelection():
                self._shift_selection(False)
            else:
                self.insertPlainText("    ")
            return
        if key == Qt.Key.Key_Backtab:
            self._shift_selection(True)
            return
        super().keyPressEvent(event)
        text = event.text()
        if text and (text.isalnum() or text in "_.@#$"):
            QTimer.singleShot(0, self.show_completions)
        elif popup.isVisible() and key == Qt.Key.Key_Backspace:
            QTimer.singleShot(0, self.show_completions)
        elif popup.isVisible():
            popup.hide()

    def _current_indent(self) -> str:
        line = self.textCursor().block().text()
        return line[: len(line) - len(line.lstrip(" \t"))]

    def _shift_selection(self, unindent: bool) -> None:
        cur = self.textCursor()
        doc = self.document()
        first = doc.findBlock(cur.selectionStart()).blockNumber()
        last = doc.findBlock(cur.selectionEnd()).blockNumber()
        cur.beginEditBlock()
        for n in range(first, last + 1):
            block = doc.findBlockByNumber(n)
            c = QTextCursor(block)
            if unindent:
                text = block.text()
                remove = min(4, len(text) - len(text.lstrip(" ")))
                if text.startswith("\t"):
                    remove = 1
                for _ in range(remove):
                    c.deleteChar()
            else:
                c.insertText("    ")
        cur.endEditBlock()

    def show_completions(self, explicit: bool = False) -> None:
        if self.completion_provider is None:
            return
        pos = self.textCursor().position()
        start, items = self.completion_provider(self.toPlainText(), pos)
        if not items:
            self._completer.popup().hide()
            return
        self._comp_start = start
        self._insert_map = {}
        shown = []
        for it in items:
            label = f"{it.text}    {KIND_LABELS.get(it.kind, it.kind)}" + (f" · {it.detail}" if it.detail else "")
            self._insert_map[label] = it.insert
            shown.append(label)
        self._model.setStringList(shown)
        self._completer.setCompletionPrefix("")
        popup = self._completer.popup()
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(min(520, popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width() + 8))
        self._completer.complete(rect)

    def _insert_completion(self, label: str) -> None:
        text = self._insert_map.get(label)
        if text is None:
            return
        cur = self.textCursor()
        cur.setPosition(self._comp_start)
        cur.setPosition(self.textCursor().position(), QTextCursor.MoveMode.KeepAnchor)
        cur.insertText(text)
        self.setTextCursor(cur)
