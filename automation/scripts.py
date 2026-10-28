"""Менеджер SQL-сценариев: хранение, параметры, подтверждение изменяющих скриптов, запуск и журнал.

Правила безопасности:
* сценарий только на чтение выполняется без дополнительных условий;
* изменяющий сценарий выполняется, только если разрешены изменения И подтверждён именно этот текст SQL
  (хеш); после любого изменения текста подтверждение аннулируется;
* сценарии из файлов импортируются неподтверждёнными и сами не запускаются.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from database.connection import SqlConnection
from database.editor_session import ExecutionListener
from database.errors import DbWorkspaceError, QueryCancelledError
from database.sql_guard import Analysis, analyze, describe
from reports.model import Param
from reports.variables import ParamError, coerce_params, convert_named_params, find_named_params
from storage.db import AppDatabase
from utils.export import open_writer
from utils.files import sanitize_filename
from utils.logging_setup import redact


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


@dataclass
class Scenario:
    id: str
    name: str
    sql: str = ""
    profile_id: str = ""
    database: str = ""
    description: str = ""
    params: list[dict] = field(default_factory=list)       # {name, label, type, default, required}
    allow_modifications: bool = False
    approved_hash: str = ""
    approved_at: str = ""
    use_transaction: bool = True
    timeout: int = 0
    save_results: bool = False
    results_dir: str = ""

    @staticmethod
    def new(name: str = "Новый сценарий", sql: str = "") -> "Scenario":
        return Scenario(uuid.uuid4().hex[:12], name, sql)

    @property
    def is_approved(self) -> bool:
        return bool(self.approved_hash) and self.approved_hash == sql_hash(self.sql)

    def param_objects(self) -> list[Param]:
        names = set(Param.__dataclass_fields__)
        return [Param(**{k: v for k, v in p.items() if k in names}) for p in self.params]


@dataclass
class ScriptRun:
    status: str = "failed"                 # ok | failed | cancelled | refused
    elapsed: float = 0.0
    rowsets: list[dict] = field(default_factory=list)       # {columns, preview, count, truncated}
    rowcounts: list[int] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    error: str = ""
    csv_path: str = ""
    analysis_level: int = 0

    def summary(self) -> str:
        if self.status == "refused":
            return self.error
        bits = [f"{self.elapsed:.1f} с"]
        if self.rowsets:
            bits.append(f"наборов: {len(self.rowsets)}, строк: {sum(r['count'] for r in self.rowsets)}")
        if self.rowcounts:
            bits.append(f"затронуто строк: {sum(self.rowcounts)}")
        if self.csv_path:
            bits.append(f"результат: {self.csv_path}")
        return ("Ошибка: " + self.error + " · " if self.error else "") + ", ".join(bits)


def check_permission(s: Scenario) -> tuple[bool, str, Analysis]:
    a = analyze(s.sql)
    if a.errors:
        return False, " ".join(a.errors), a
    if not a.batches:
        return False, "Сценарий пуст.", a
    if a.is_read_only:
        return True, "", a
    if not s.allow_modifications:
        return False, "Сценарий изменяет данные или схему, а изменения для него не разрешены.\n" + describe(a), a
    if not s.is_approved:
        return False, ("Текст изменяющего сценария не подтверждён (или изменён после подтверждения). "
                       "Проверьте SQL и подтвердите его заново."), a
    return True, "", a


def approve(s: Scenario) -> Scenario:
    if not s.allow_modifications:
        raise ValueError("Сначала разрешите изменения для этого сценария.")
    s.approved_hash = sql_hash(s.sql)
    s.approved_at = dt.datetime.now().isoformat(timespec="seconds")
    return s


class _Collect(ExecutionListener):
    def __init__(self, preview: int, keep_first: int):
        self.preview, self.keep_first = preview, keep_first
        self.rowsets: list[dict] = []
        self.first_rows: list[tuple] = []
        self.first_columns: list[str] = []
        self.rowcounts: list[int] = []
        self.messages: list[str] = []
        self._cur: dict | None = None

    def on_result(self, index, columns):
        self._cur = {"columns": list(columns), "preview": [], "count": 0, "truncated": False}
        self.rowsets.append(self._cur)
        if len(self.rowsets) == 1:
            self.first_columns = list(columns)
            return self.keep_first
        return self.preview

    def on_rows(self, index, rows):
        if self._cur is None:
            return
        room = self.preview - len(self._cur["preview"])
        if room > 0:
            self._cur["preview"].extend([list(map(_jsonable, r)) for r in rows[:room]])
        self._cur["count"] += len(rows)
        if len(self.rowsets) == 1:
            self.first_rows.extend(rows)

    def on_result_done(self, index, count, truncated):
        if self._cur is not None:
            self._cur["truncated"] = truncated

    def on_rowcount(self, index, affected):
        self.rowcounts.append(affected)

    def on_messages(self, messages):
        self.messages.extend(messages)


def _jsonable(v):
    from utils.export import json_value
    return json_value(v)


def run_scenario(conn: SqlConnection, s: Scenario, raw_params: dict[str, str] | None = None, *, cancel_token=None,
                 preview_rows: int = 20, csv_cap: int = 100_000) -> ScriptRun:
    started = time.perf_counter()
    run = ScriptRun()
    ok, reason, a = check_permission(s)
    run.analysis_level = int(a.level)
    if not ok:
        run.status, run.error = "refused", reason
        return run
    try:
        values = coerce_params(s.param_objects(), raw_params or {})
    except ParamError as exc:
        run.error = str(exc)
        return run
    if values and len(a.batches) > 1:
        run.error = "Параметры :имя поддерживаются только в сценариях из одного пакета (без GO)."
        return run
    used = set()
    for b in a.batches:
        used.update(find_named_params(b.text))
    unknown = used - set(values)
    if unknown:
        run.error = "В SQL есть параметры, не описанные в сценарии: " + ", ".join(sorted(":" + u for u in unknown))
        return run
    modifying = not a.is_read_only
    tx = modifying and s.use_transaction
    col = _Collect(preview_rows, csv_cap if s.save_results else preview_rows)
    if s.timeout:
        conn.set_query_timeout(s.timeout)
    try:
        if tx:
            conn.set_autocommit(False)
        for b in a.batches:
            sql, vals = convert_named_params(b.text, values) if values else (b.text, [])
            conn.execute_multi(sql, vals, listener=col, cancel_token=cancel_token)
        if tx:
            conn.commit()
        run.status = "ok"
    except QueryCancelledError:
        run.status, run.error = "cancelled", "Выполнение отменено."
        _safe_rollback(conn, tx, run)
    except DbWorkspaceError as exc:
        run.error = str(exc)
        _safe_rollback(conn, tx, run)
    finally:
        if tx:
            try:
                conn.set_autocommit(True)
            except DbWorkspaceError:
                pass
    run.rowsets, run.rowcounts = col.rowsets, col.rowcounts
    run.messages = col.messages + run.messages          # сообщения сервера + наши (например, об откате)
    if run.status == "ok" and s.save_results and s.results_dir and col.first_columns:
        try:
            folder = Path(s.results_dir)
            path = folder / f"{sanitize_filename(s.name, 'scenario')}_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
            w = open_writer(path, "csv", col.first_columns)
            w.write_rows(col.first_rows)
            w.close()
            run.csv_path = str(path)
        except Exception as exc:  # noqa: BLE001
            run.messages.append(f"Не удалось сохранить результат в CSV: {exc}")
    run.elapsed = time.perf_counter() - started
    return run


def _safe_rollback(conn: SqlConnection, tx: bool, run: ScriptRun) -> None:
    if not tx:
        if run.rowcounts:
            run.messages.append("Сценарий выполнялся без транзакции: изменения, сделанные до ошибки, сохранены.")
        return
    try:
        conn.rollback()
        run.messages.append("Транзакция откачена: изменения сценария не сохранены.")
    except DbWorkspaceError:
        run.messages.append("Не удалось откатить транзакцию (соединение потеряно — сервер откатит её сам).")


class ScenarioStore:
    def __init__(self, db: AppDatabase):
        self.db = db

    def list(self) -> list[Scenario]:
        return [self._load(r["data"]) for r in self.db.query("SELECT data FROM script_scenarios ORDER BY name COLLATE NOCASE")]

    @staticmethod
    def _load(text: str) -> Scenario:
        d = json.loads(text)
        names = set(Scenario.__dataclass_fields__)
        return Scenario(**{k: v for k, v in d.items() if k in names})

    def get(self, sid: str) -> Scenario | None:
        rows = self.db.query("SELECT data FROM script_scenarios WHERE id=?", (sid,))
        return self._load(rows[0]["data"]) if rows else None

    def save(self, s: Scenario) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        if s.approved_hash and s.approved_hash != sql_hash(s.sql):
            s.approved_hash, s.approved_at = "", ""          # текст изменён — подтверждение снимается
        data = json.dumps(asdict(s), ensure_ascii=False)
        if self.get(s.id) is None:
            self.db.execute("INSERT INTO script_scenarios(id, name, data, created_at, updated_at) VALUES(?,?,?,?,?)",
                            (s.id, s.name, data, now, now))
        else:
            self.db.execute("UPDATE script_scenarios SET name=?, data=?, updated_at=? WHERE id=?", (s.name, data, now, s.id))

    def delete(self, sid: str) -> None:
        self.db.execute("DELETE FROM script_scenarios WHERE id=?", (sid,))
        self.db.execute("DELETE FROM script_runs WHERE scenario_id=?", (sid,))

    def import_sql_file(self, path: Path) -> Scenario:
        from utils.files import read_text_auto
        text, _enc = read_text_auto(path)
        s = Scenario.new(Path(path).stem, text)             # импортированный сценарий не подтверждён
        params = [{"name": n, "label": n, "type": "text", "default": "", "required": True} for n in find_named_params(text)]
        s.params = params
        self.save(s)
        return s

    def log_run(self, sid: str, run: ScriptRun) -> int:
        details = json.dumps({"rowsets": [{k: v for k, v in r.items()} for r in run.rowsets[:5]],
                              "rowcounts": run.rowcounts[:50], "messages": run.messages[:50], "csv": run.csv_path},
                             ensure_ascii=False, default=str)
        rid = self.db.execute("INSERT INTO script_runs(scenario_id, ts, status, duration_ms, rows, message, details) "
                              "VALUES(?,?,?,?,?,?,?)",
                              (sid, dt.datetime.now().isoformat(timespec="seconds"), run.status, int(run.elapsed * 1000),
                               sum(r["count"] for r in run.rowsets) + sum(run.rowcounts), redact(run.summary())[:2000],
                               redact(details)[:50000]))
        self.db.execute("DELETE FROM script_runs WHERE scenario_id=? AND id NOT IN (SELECT id FROM script_runs "
                        "WHERE scenario_id=? ORDER BY id DESC LIMIT 200)", (sid, sid))
        return rid

    def runs(self, sid: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM script_runs WHERE scenario_id=? ORDER BY id DESC LIMIT ?",
                                               (sid, limit))]
