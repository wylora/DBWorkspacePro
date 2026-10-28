"""Мониторинг: доступность, отклик, размеры, таблицы, активность (если есть права), история показателей."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from database.errors import DbWorkspaceError
from monitoring.collect import MonitorSnapshot, Probe, collect, metrics_from
from reports.charts import ChartData, render_chart
from reports.formatting import Formatter
from reports.styles import get_style
from utils.tasks import DuplicateTaskError

from .automation_page import fill, table
from .context import AppContext
from .widgets import PageHeader, StatCard


class MonitoringPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self.auto = ctx.automation
        self.snap: MonitorSnapshot | None = None
        self._build()
        ctx.connections_changed.connect(self._reload_profiles)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_tick)
        self._apply_interval()
        self.timer.start()
        self._reload_profiles()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Мониторинг", "Показатели, доступные вашей учётной записи. Если данных или прав нет, показатель не "
                          "выводится — вместо него заметка. Значения сохраняются для графиков истории."))
        bar = QHBoxLayout()
        self.c_conn = QComboBox()
        self.c_conn.setMinimumWidth(200)
        self.e_db = QLineEdit()
        self.e_db.setPlaceholderText("база данных (для таблиц)")
        self.k_auto = QCheckBox("Автообновление")
        self.b_refresh = QPushButton("Обновить")
        self.b_refresh.setObjectName("Primary")
        self.b_refresh.clicked.connect(self.refresh)
        self.lbl_updated = QLabel("Данные ещё не получены")
        self.lbl_updated.setObjectName("Muted")
        for w in (QLabel("Подключение:"), self.c_conn, QLabel("База:"), self.e_db, self.k_auto, self.b_refresh,
                  self.lbl_updated):
            bar.addWidget(w)
        bar.addStretch(1)
        root.addLayout(bar)
        cards = QHBoxLayout()
        self.c_avail = StatCard("Доступность")
        self.c_lat = StatCard("Время отклика")
        self.c_size = StatCard("Размер базы")
        self.c_tables = StatCard("Таблиц в базе")
        for c in (self.c_avail, self.c_lat, self.c_size, self.c_tables):
            cards.addWidget(c)
        root.addLayout(cards)
        self.tabs = QTabWidget()
        self.t_tables = table(["Схема", "Таблица", "Строк", "Размер, МБ"])
        self.t_dbs = table(["База", "Данные, МБ", "Журнал, МБ"])
        self.t_req = table(["Сеанс", "Состояние", "Команда", "База", "Ожидание", "Длит., мс", "Блокирует сеанс",
                            "Пользователь", "Компьютер", "Текст"])
        self.t_locks = table(["Сеанс", "Ресурс", "База", "Режим", "Статус", "Количество"])
        self.chart = QLabel("Недостаточно данных для графика: обновите показатели несколько раз.")
        self.chart.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.app_stats = QPlainTextEdit()
        self.app_stats.setReadOnly(True)
        for w, title in ((self.t_tables, "Крупнейшие таблицы"), (self.t_dbs, "Базы данных"), (self.t_req, "Активные запросы"),
                         (self.t_locks, "Блокировки"), (self.chart, "История отклика"), (self.app_stats, "Приложение")):
            self.tabs.addTab(w, title)
        root.addWidget(self.tabs, 1)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(90)
        self.notes.setPlaceholderText("Здесь будут заметки о недоступных показателях")
        root.addWidget(self.notes)

    def _reload_profiles(self) -> None:
        cur = self.c_conn.currentData()
        self.c_conn.clear()
        for p in self.svc.profiles.repo.list():
            self.c_conn.addItem(p.name, p.id)
        self.c_conn.setCurrentIndex(max(0, self.c_conn.findData(cur)))
        if not self.e_db.text():
            prof = self.svc.profiles.repo.get(self.c_conn.currentData() or "")
            if prof:
                self.e_db.setText(prof.database)

    def _apply_interval(self) -> None:
        sec = self.svc.settings.get("monitor_interval")
        self.timer.setInterval(sec * 1000)
        self.k_auto.setText(f"Автообновление каждые {sec} с")

    def _auto_tick(self) -> None:
        if self.k_auto.isChecked() and self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        pid = self.c_conn.currentData()
        if not pid:
            self.lbl_updated.setText("Нет сохранённых подключений.")
            return
        db = self.e_db.text().strip()
        profiles, metrics = self.svc.profiles, self.auto.metrics

        def job(c):
            try:
                with profiles.lease(pid, "monitor") as conn:
                    snap = collect(conn, db)
            except DbWorkspaceError as exc:
                snap = MonitorSnapshot(dt.datetime.now(), Probe(False, None, str(exc)), db,
                                       notes=["Не удалось подключиться к серверу."])
            metrics.add_many(pid, metrics_from(snap))
            return snap

        try:
            self.ctx.runner.submit("Мониторинг: сбор показателей", job, on_success=self.show_snapshot,
                                   on_error=lambda m: self.lbl_updated.setText("Ошибка: " + m), key=f"monitor:{pid}")
        except DuplicateTaskError:
            return
        self.lbl_updated.setText("Сбор показателей…")

    def show_snapshot(self, snap: MonitorSnapshot) -> None:
        self.snap = snap
        p = snap.probe
        self.lbl_updated.setText(f"Обновлено: {snap.ts:%H:%M:%S}")
        self.c_avail.set_value("доступен" if p.ok else "НЕДОСТУПЕН", "" if p.ok else p.error[:120])
        self.c_lat.set_value(f"{p.latency_ms:.0f} мс" if p.latency_ms is not None else "—")
        size = next((d + l for n, d, l in snap.db_sizes or [] if n == snap.database), None)
        self.c_size.set_value(f"{size:,.1f} МБ".replace(",", " ") if size is not None else "—",
                              "нет данных или прав" if size is None and p.ok else "")
        self.c_tables.set_value(str(snap.table_count) if snap.table_count is not None else "—")
        fill(self.t_tables, [[t.schema, t.name, f"{t.rows:,}".replace(",", " "),
                              "—" if t.size_mb is None else f"{t.size_mb:.2f}"] for t in snap.tables or []])
        fill(self.t_dbs, [[n, f"{d:.1f}", f"{l:.1f}"] for n, d, l in snap.db_sizes or []])
        fill(self.t_req, [[r["session"], r["status"], r["command"], r["database"], r["wait_type"] or "", r["elapsed_ms"],
                           r["blocked_by"] or "", r["login"], r["host"], r["text"]] for r in snap.requests or []])
        fill(self.t_locks, [[l["session"], l["resource"], l["database"], l["mode"], l["status"], l["count"]]
                            for l in snap.locks or []])
        self.notes.setPlainText("\n".join(snap.notes))
        self._update_chart()
        self._update_app_stats()

    def _update_chart(self) -> None:
        pid = self.c_conn.currentData()
        series = [(ts, v) for ts, v in self.auto.metrics.series(pid, "latency_ms", "", 60) if v is not None]
        if len(series) < 2:
            self.chart.setPixmap(QPixmap())
            self.chart.setText("Недостаточно данных для графика: обновите показатели несколько раз.")
            return
        data = ChartData([ts[11:16] for ts, _ in series], {"Отклик, мс": [float(v) for _, v in series]}, True)
        png = render_chart(data, "line", "", get_style("analytic"), Formatter("ru"), 8, 2.8)
        pix = QPixmap()
        pix.loadFromData(png, "PNG")
        self.chart.setPixmap(pix)

    def _update_app_stats(self) -> None:
        errors = self.svc.oplog.count_since(24, "error")
        recent = [r for r in self.svc.oplog.recent(200) if r.status == "error"][:8]
        durations = [h.duration_ms for h in self.svc.history.recent(200) if h.duration_ms is not None]
        lines = [f"Ошибок приложения за 24 часа: {errors}"]
        lines += [f"  {r.ts.replace('T', ' ')} · {r.category} · {r.action}: {r.message[:120]}" for r in recent]
        if durations:
            lines.append("")
            lines.append(f"Запросы SQL-редактора (последние {len(durations)}): среднее {sum(durations) / len(durations):.0f} мс, "
                         f"максимум {max(durations)} мс")
        else:
            lines.append("Истории запросов SQL-редактора пока нет.")
        self.app_stats.setPlainText("\n".join(lines))

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._apply_interval()
