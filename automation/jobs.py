"""Исполнители задач автоматизации. execute() никогда не бросает исключение — возвращает Outcome."""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bootstrap import AppServices
from comparison.data_diff import DiffError, diff_tables, totals_diff
from database.editor_session import ExecutionListener
from database.errors import DbWorkspaceError, QueryCancelledError, StopExecution
from database.sql_guard import analyze
from database.sqltext import qualified_name
from monitoring.collect import Probe, collect, metrics_from, probe
from utils.export import EXTENSIONS, ExportError, open_writer
from utils.files import sanitize_filename

from .backup import BackupOptions, apply_cleanup, create_backup, plan_cleanup
from .schedule import validate as validate_schedule
from .scripts import ScenarioStore, check_permission, run_scenario
from .snapshots import SnapshotStore
from .store import MetricStore, ScheduledTask

log = logging.getLogger("dbworkspace.automation")
ON_CONNECT_KINDS = ("health", "table_stats", "report", "export", "script")
NOTIFY = ("errors", "always", "never")
_TABLE = re.compile(r"^\s*(?:\[?(?P<s>[^\].]+)\]?\.)?\[?(?P<t>[^\]]+?)\]?\s*$")


@dataclass
class Outcome:
    status: str                        # ok | warning | failed | cancelled
    message: str
    details: str = ""
    notify: bool | None = None         # None — по правилу задачи
    files: list[str] = field(default_factory=list)


@dataclass
class JobContext:
    services: AppServices
    metrics: MetricStore
    scenarios: ScenarioStore
    snapshots: SnapshotStore
    cancel_token: object = None
    progress: Callable[[str, float | None], None] = lambda stage, frac: None
    trigger: str = "manual"
    previous_status: str | None = None


def parse_table(text: str) -> tuple[str, str]:
    m = _TABLE.match(text or "")
    if not m or not m.group("t").strip():
        raise ValueError(f"Неверное имя таблицы: «{text}» (ожидается схема.таблица).")
    return (m.group("s") or "dbo").strip(), m.group("t").strip()


# ================================================================== проверка
def validate_task(t: ScheduledTask, svc: AppServices, scenarios: ScenarioStore | None = None) -> list[str]:
    errs = []
    if not t.name.strip():
        errs.append("Укажите название задачи.")
    errs += validate_schedule(t.schedule)
    c = t.config
    if c.get("notify", "errors") not in NOTIFY:
        errs.append("Неизвестный режим уведомлений.")
    pid = c.get("profile_id", "")
    if t.kind != "script":
        if not pid or svc.profiles.repo.get(pid) is None:
            errs.append("Выберите сохранённое подключение.")
    if t.schedule.kind == "on_connect" and t.kind not in ON_CONNECT_KINDS:
        errs.append("Запуск после подключения разрешён только для задач чтения (не для резервного копирования).")
    if t.kind == "report":
        from reports.service import ReportService
        try:
            ReportService(svc).templates.load(c.get("template_id", ""))
        except Exception:  # noqa: BLE001
            errs.append("Выберите существующий шаблон отчёта.")
        from reports.model import FORMATS
        fm = c.get("formats") or []
        if not fm or any(f not in FORMATS for f in fm):
            errs.append("Выберите форматы отчёта.")
        if not str(c.get("output_dir", "")).strip():
            errs.append("Укажите папку для отчётов.")
    elif t.kind == "export":
        tables = c.get("tables") or []
        if not tables:
            errs.append("Укажите хотя бы одну таблицу (схема.таблица).")
        for x in tables:
            try:
                parse_table(x)
            except ValueError as exc:
                errs.append(str(exc))
        if c.get("format", "csv") not in EXTENSIONS:
            errs.append("Формат экспорта: csv, xlsx или json.")
        if not str(c.get("output_dir", "")).strip():
            errs.append("Укажите папку для файлов экспорта.")
        if not str(c.get("database", "")).strip():
            errs.append("Укажите базу данных.")
    elif t.kind == "backup":
        dbs = c.get("databases") or []
        if not dbs:
            errs.append("Укажите базы данных для копирования.")
        if not str(c.get("folder", "")).strip():
            errs.append("Укажите папку на сервере для резервных копий.")
        cl = c.get("cleanup") or {}
        if cl.get("enabled"):
            if not cl.get("acknowledged"):
                errs.append("Подтвердите удаление старых копий (флажок согласия) или выключите очистку.")
            if int(cl.get("keep_last", 0)) < 1:
                errs.append("При очистке нужно хранить не меньше одной последней копии.")
    elif t.kind == "health":
        if int(c.get("latency_warn_ms", 1000)) < 1:
            errs.append("Порог времени отклика должен быть положительным.")
    elif t.kind == "table_stats":
        if not str(c.get("database", "")).strip():
            errs.append("Укажите базу данных.")
    elif t.kind == "script":
        s = scenarios.get(c.get("scenario_id", "")) if scenarios else None
        if s is None:
            errs.append("Выберите существующий SQL-сценарий.")
        else:
            if svc.profiles.repo.get(s.profile_id) is None:
                errs.append("У сценария не выбрано подключение.")
            ok, reason, a = check_permission(s)
            if not ok:
                errs.append(reason.split("\n")[0])
            if t.schedule.kind == "on_connect" and not a.is_read_only:
                errs.append("После подключения можно запускать только сценарии на чтение.")
    else:
        errs.append(f"Неизвестный вид задачи: {t.kind}.")
    return errs


# ================================================================ исполнители
def _report(t: ScheduledTask, ctx: JobContext) -> Outcome:
    from reports.generator import ReportCancelled
    from reports.service import ReportService
    c = t.config
    rs = ReportService(ctx.services)
    template = rs.templates.load(c["template_id"])
    req = rs.build_request(template, dict(c.get("params") or {}), list(c["formats"]), c["profile_id"],
                           c.get("database", ""), output_dir=c["output_dir"], overwrite=c.get("policy", "new_version"))
    req.keep_datasets = bool(c.get("keep_history"))
    try:
        res = rs.run(req, progress=ctx.progress, cancel_token=ctx.cancel_token)
    except ReportCancelled:
        return Outcome("cancelled", "Формирование отчёта отменено.")
    files = [str(f.path) for f in res.files]
    lines = [f"Файлы: {', '.join(Path(f).name for f in files) or 'нет'}"]
    lines += [f"Не создан {k}: {v}" for k, v in res.errors.items()]
    changes = []
    if req.keep_datasets:
        keys = c.get("compare_keys") or {}
        for sid, ds in res.datasets.items():
            ctx.snapshots.save(t.id, sid, ds.columns, ds.rows)
            prev, latest = ctx.snapshots.previous_and_latest(t.id, sid)
            if prev is None:
                lines.append(f"«{ds.name}»: первый снимок данных сохранён — сравнение будет со следующего запуска.")
                continue
            key = keys.get(sid) or []
            if key:
                try:
                    d = diff_tables(prev["columns"], prev["rows"], latest["columns"], latest["rows"], key)
                    lines.append(f"«{ds.name}» по сравнению с {prev['ts'].replace('T', ' ')}: {d.summary()}")
                    if d.has_changes:
                        changes.append(ds.name)
                except DiffError as exc:
                    lines.append(f"«{ds.name}»: сравнение по ключу невозможно — {exc}")
            else:
                tot = totals_diff(prev["columns"], prev["rows"], latest["columns"], latest["rows"])
                delta = len(latest["rows"]) - len(prev["rows"])
                diffs = [f"сумма «{k}»: {a} → {b}" for k, (a, b) in tot.items() if a != b]
                lines.append(f"«{ds.name}»: строк {len(prev['rows'])} → {len(latest['rows'])} ({delta:+d})"
                             + ("; " + "; ".join(diffs) if diffs else "") + " (ключ для построчного сравнения не задан)")
                if delta or diffs:
                    changes.append(ds.name)
    status = "ok" if res.ok and not res.errors else ("warning" if res.ok else "failed")
    msg = f"Отчёт «{template.name}»: файлов {len(files)}" + (f"; изменились данные: {', '.join(changes)}" if changes else "")
    return Outcome(status, msg, "\n".join(lines + res.warnings[:20]), files=files)


class _ExportListener(ExecutionListener):
    def __init__(self, path: Path, fmt: str, options: dict):
        self.path, self.fmt, self.options = path, fmt, options
        self.writer = None
        self.rows = 0

    def on_result(self, index, columns):
        if self.writer is None:
            self.writer = open_writer(self.path, self.fmt, columns, **self.options)
            return None
        return 0

    def on_rows(self, index, rows):
        if self.writer is not None:
            self.writer.write_rows(rows)
            self.rows += len(rows)

    def on_result_done(self, index, count, truncated):
        raise StopExecution()


def _unique(path: Path) -> Path:
    n, cand = 1, path
    while cand.exists():
        n += 1
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
    return cand


def _export(t: ScheduledTask, ctx: JobContext) -> Outcome:
    c = t.config
    fmt = c.get("format", "csv")
    out = Path(c["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    s = ctx.services.settings
    opts = {"delimiter": s.get("export_csv_delimiter"), "formula_guard": s.get("export_formula_guard")}
    top = int(c.get("max_rows") or 0)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    done, failed, files = [], [], []
    with ctx.services.profiles.lease(c["profile_id"], "export") as conn:
        tables = c["tables"]
        for i, raw in enumerate(tables):
            if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                return Outcome("cancelled", f"Экспорт отменён (готово таблиц: {len(done)}).", "\n".join(done), files=files)
            schema, name = parse_table(raw)
            ctx.progress(f"Экспорт {schema}.{name}", i / len(tables))
            sql = f"SELECT {'TOP (' + str(top) + ') ' if top > 0 else ''}* FROM {qualified_name(c['database'], schema, name)}"
            path = _unique(out / f"{sanitize_filename(f'{schema}.{name}', 'table')}_{stamp}{EXTENSIONS[fmt]}")
            lst = _ExportListener(path, fmt, opts)
            try:
                try:
                    conn.execute_multi(sql, listener=lst, cancel_token=ctx.cancel_token)
                except StopExecution:
                    pass
                if lst.writer is None:
                    raise ExportError("запрос не вернул данных")
                r = lst.writer.close()
                files.append(str(path))
                done.append(f"{schema}.{name}: {r.rows} строк → {path.name}")
            except QueryCancelledError:
                if lst.writer is not None:
                    lst.writer.abort()
                return Outcome("cancelled", "Экспорт отменён.", "\n".join(done), files=files)
            except (DbWorkspaceError, ExportError, OSError) as exc:
                if lst.writer is not None:
                    lst.writer.abort()
                failed.append(f"{schema}.{name}: {exc}")
    status = "ok" if not failed else ("warning" if done else "failed")
    return Outcome(status, f"Экспортировано таблиц: {len(done)} из {len(c['tables'])}",
                   "\n".join(done + ["ОШИБКИ:"] + failed if failed else done), files=files)


def _backup(t: ScheduledTask, ctx: JobContext) -> Outcome:
    c = t.config
    lines, failed, warn, files = [], [], False, []
    with ctx.services.profiles.lease(c["profile_id"], "backup") as conn:
        for i, db in enumerate(c["databases"]):
            if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                return Outcome("cancelled", "Резервное копирование отменено.", "\n".join(lines))
            ctx.progress(f"Резервное копирование: {db}", i / len(c["databases"]))
            o = BackupOptions(db, c["folder"], bool(c.get("copy_only", True)), bool(c.get("checksum", True)),
                              bool(c.get("compression", False)), bool(c.get("verify", True)), int(c.get("timeout", 0)))
            r = create_backup(conn, o, cancel_token=ctx.cancel_token)
            if not r.ok:
                failed.append(f"{db}: {r.error}")
                continue
            files.append(r.path)
            size = f", {r.size_bytes / 1048576:.1f} МБ" if r.size_bytes else ""
            ver = {True: "проверена", False: "проверка не прошла", None: "не проверялась"}[r.verified]
            lines.append(f"{db}: {r.path}{size} ({ver})")
            lines += ["   " + n for n in r.notes]
            warn = warn or r.verified is None
            cl = c.get("cleanup") or {}
            if cl.get("enabled") and cl.get("acknowledged"):
                folder = Path(cl.get("local_folder") or c["folder"])
                try:
                    plan = plan_cleanup(folder, db, keep_last=int(cl.get("keep_last", 7)),
                                        older_than_days=int(cl.get("older_than_days", 0)))
                    deleted, errors = apply_cleanup(plan)
                    lines.append(f"   очистка: удалено старых копий — {len(deleted)}")
                    if errors:
                        warn = True
                        lines += ["   ошибка удаления: " + e for e in errors]
                except Exception as exc:  # noqa: BLE001
                    warn = True
                    lines.append(f"   очистка не выполнена: {exc}")
    status = "failed" if failed and not lines else ("warning" if failed or warn else "ok")
    msg = f"Резервных копий создано: {len(files)} из {len(c['databases'])}"
    return Outcome(status, msg, "\n".join(lines + (["ОШИБКИ:"] + failed if failed else [])), files=files)


def _health(t: ScheduledTask, ctx: JobContext) -> Outcome:
    c = t.config
    pid = c["profile_id"]
    try:
        with ctx.services.profiles.lease(pid, "health") as conn:
            p = probe(conn)
    except DbWorkspaceError as exc:
        p = Probe(False, None, str(exc))
    ctx.metrics.add_many(pid, [("available", "", 1.0 if p.ok else 0.0)] +
                         ([("latency_ms", "", p.latency_ms)] if p.latency_ms is not None else []))
    name = ctx.services.profiles.repo.get(pid).name if ctx.services.profiles.repo.get(pid) else pid
    if not p.ok:
        return Outcome("failed", f"Сервер «{name}» недоступен: {p.error}")
    warn = int(c.get("latency_warn_ms", 1000))
    recovered = ctx.previous_status == "failed"
    if p.latency_ms is not None and p.latency_ms > warn:
        return Outcome("warning", f"Сервер «{name}» отвечает медленно: {p.latency_ms:.0f} мс (порог {warn} мс)",
                       notify=True if recovered else None)
    return Outcome("ok", f"Сервер «{name}» доступен, отклик {p.latency_ms:.0f} мс"
                   + (" — снова доступен после сбоя" if recovered else ""), notify=True if recovered else None)


def _table_stats(t: ScheduledTask, ctx: JobContext) -> Outcome:
    c = t.config
    with ctx.services.profiles.lease(c["profile_id"], "stats") as conn:
        snap = collect(conn, c["database"], top_tables=int(c.get("top", 20)), include_activity=False)
    ctx.metrics.add_many(c["profile_id"], metrics_from(snap))
    if not snap.probe.ok:
        return Outcome("failed", f"Сервер недоступен: {snap.probe.error}")
    size = next((d + l for n, d, l in snap.db_sizes or [] if n == c["database"]), None)
    msg = f"База «{c['database']}»: таблиц {snap.table_count if snap.table_count is not None else '—'}" + \
          (f", размер {size:.1f} МБ" if size is not None else "")
    details = "\n".join(f"{x.schema}.{x.name}: {x.rows} строк" + (f", {x.size_mb:.2f} МБ" if x.size_mb is not None else "")
                        for x in snap.tables or [])
    return Outcome("warning" if snap.notes and snap.tables is None else "ok", msg, details + "\n" + "\n".join(snap.notes))


def _script(t: ScheduledTask, ctx: JobContext) -> Outcome:
    s = ctx.scenarios.get(t.config.get("scenario_id", ""))
    if s is None:
        return Outcome("failed", "SQL-сценарий не найден (возможно, удалён).")
    with ctx.services.profiles.lease(s.profile_id, "script") as conn:
        if s.database:
            from database.sqltext import quote_ident
            conn.execute_multi("USE " + quote_ident(s.database), listener=ExecutionListener())
        run = run_scenario(conn, s, dict(t.config.get("params") or {}), cancel_token=ctx.cancel_token)
    ctx.scenarios.log_run(s.id, run)
    status = {"ok": "ok", "cancelled": "cancelled"}.get(run.status, "failed")
    return Outcome(status, f"Сценарий «{s.name}»: {run.summary()}", "\n".join(run.messages[:50]),
                   files=[run.csv_path] if run.csv_path else [])


EXECUTORS = {"report": _report, "export": _export, "backup": _backup, "health": _health, "table_stats": _table_stats,
             "script": _script}


def execute(t: ScheduledTask, ctx: JobContext) -> Outcome:
    fn = EXECUTORS.get(t.kind)
    if fn is None:
        return Outcome("failed", f"Неизвестный вид задачи: {t.kind}")
    try:
        return fn(t, ctx)
    except QueryCancelledError:
        return Outcome("cancelled", "Выполнение отменено.")
    except (DbWorkspaceError, ValueError, OSError, KeyError) as exc:
        return Outcome("failed", str(exc) if not isinstance(exc, KeyError) else f"В настройках задачи нет параметра {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой задачи %s", t.name)
        return Outcome("failed", f"{exc.__class__.__name__}: {exc}")
