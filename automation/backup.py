"""Резервное копирование SQL Server: создание, проверка, история, очистка, восстановление в новую базу.

Важно: путь резервной копии — путь НА СЕРВЕРЕ (файл пишет служба SQL Server от своей учётной записи).
Проверить файл с этого компьютера можно, только если папка доступна (локальный сервер или общая папка);
иначе используется RESTORE VERIFYONLY. Права: обычно нужна роль db_backupoperator (или выше) в базе.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from database.connection import SqlConnection
from database.editor_session import ExecutionListener
from database.errors import DbWorkspaceError
from utils.files import sanitize_filename

log = logging.getLogger("dbworkspace.backup")
SYSTEM_DBS = {"master", "model", "msdb", "tempdb"}


class BackupError(DbWorkspaceError):
    pass


@dataclass
class BackupOptions:
    database: str
    folder: str                       # папка на сервере
    copy_only: bool = True            # не нарушает цепочку резервных копий, настроенную администратором
    checksum: bool = True
    compression: bool = False         # в Express/Web недоступно
    verify: bool = True
    timeout: int = 0                  # 0 — без ограничения


@dataclass
class BackupResult:
    database: str
    path: str
    started: dt.datetime
    finished: dt.datetime | None = None
    ok: bool = False
    verified: bool | None = None      # None — проверка не выполнялась/недоступна
    size_bytes: int | None = None
    file_visible: bool | None = None  # виден ли файл с этого компьютера
    messages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""


class _Messages(ExecutionListener):
    def __init__(self):
        self.messages: list[str] = []
        self.rows: list[tuple] = []
        self.columns: list[str] = []

    def on_result(self, index, columns):
        if not self.columns:
            self.columns = list(columns)
        return None

    def on_rows(self, index, rows):
        self.rows.extend(rows)

    def on_messages(self, messages):
        self.messages.extend(messages)


def backup_filename(database: str, when: dt.datetime) -> str:
    return f"{sanitize_filename(database, 'db')}_{when:%Y%m%d_%H%M%S}.bak"


def join_server_path(folder: str, name: str) -> str:
    folder = folder.strip()
    if not folder:
        raise BackupError("Не указана папка для резервной копии на сервере.")
    windows = "\\" in folder or re.match(r"^[A-Za-z]:", folder) or folder.startswith("\\\\")
    sep = "\\" if windows else "/"
    return folder.rstrip("\\/") + sep + name


def validate_options(o: BackupOptions) -> list[str]:
    errs = []
    if not o.database.strip():
        errs.append("Не выбрана база данных.")
    if o.database.lower() == "tempdb":
        errs.append("tempdb не копируется (она пересоздаётся при каждом запуске сервера).")
    if not o.folder.strip():
        errs.append("Укажите папку на сервере, куда сохранить копию.")
    if any(c in o.folder for c in "\"*?<>|\x00"):
        errs.append("Путь к папке содержит недопустимые символы.")
    return errs


def backup_sql(o: BackupOptions, path: str, name: str) -> tuple[str, list]:
    opts = ["INIT", "STATS = 10"]
    if o.copy_only:
        opts.insert(0, "COPY_ONLY")
    if o.checksum:
        opts.append("CHECKSUM")
    if o.compression:
        opts.append("COMPRESSION")
    sql = ("DECLARE @db sysname = ?, @path nvarchar(4000) = ?, @name nvarchar(128) = ?;\n"
           f"BACKUP DATABASE @db TO DISK = @path WITH {', '.join(opts)}, NAME = @name;")
    return sql, [o.database, path, name]


VERIFY_SQL = "DECLARE @path nvarchar(4000) = ?;\nRESTORE VERIFYONLY FROM DISK = @path WITH CHECKSUM;"
VERIFY_SQL_NOCHECKSUM = "DECLARE @path nvarchar(4000) = ?;\nRESTORE VERIFYONLY FROM DISK = @path;"
FILELIST_SQL = "DECLARE @path nvarchar(4000) = ?;\nRESTORE FILELISTONLY FROM DISK = @path;"
HISTORY_SQL = (
    "SELECT TOP (?) bs.database_name, bs.backup_start_date, bs.backup_finish_date, bs.type, bs.is_copy_only, "
    "bs.backup_size, bs.compressed_backup_size, bs.has_backup_checksums, bmf.physical_device_name "
    "FROM msdb.dbo.backupset bs JOIN msdb.dbo.backupmediafamily bmf ON bmf.media_set_id = bs.media_set_id "
    "WHERE bs.database_name = ? ORDER BY bs.backup_finish_date DESC")
DEFAULT_PATHS_SQL = ("SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(4000)), "
                     "CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(4000))")
BACKUP_TYPES = {"D": "полная", "I": "разностная", "L": "журнал", "F": "файловая"}


def _exec(conn: SqlConnection, sql: str, params: list, cancel_token=None) -> _Messages:
    lst = _Messages()
    conn.execute_multi(sql, params, listener=lst, cancel_token=cancel_token)
    return lst


def local_view(path: str) -> Path | None:
    """Путь, по которому файл можно проверить с этого компьютера (общая папка или локальный диск), либо None."""
    p = Path(path)
    try:
        return p if p.parent.exists() else None
    except OSError:
        return None


def create_backup(conn: SqlConnection, o: BackupOptions, *, when: dt.datetime | None = None, cancel_token=None
                  ) -> BackupResult:
    errs = validate_options(o)
    if errs:
        raise BackupError(" ".join(errs))
    when = when or dt.datetime.now()
    name = backup_filename(o.database, when)
    path = join_server_path(o.folder, name)
    res = BackupResult(o.database, path, when)
    if not conn.autocommit:
        raise BackupError("Резервное копирование нельзя выполнять внутри транзакции.")
    conn.set_query_timeout(o.timeout)
    sql, params = backup_sql(o, path, f"DBWorkspacePro {o.database} {when:%Y-%m-%d %H:%M}")
    try:
        m = _exec(conn, sql, params, cancel_token)
        res.messages = m.messages
    except DbWorkspaceError as exc:
        res.error = _backup_hint(str(exc))
        res.finished = dt.datetime.now()
        return res
    res.ok = True
    res.finished = dt.datetime.now()
    if o.verify:
        try:
            _exec(conn, VERIFY_SQL if o.checksum else VERIFY_SQL_NOCHECKSUM, [path], cancel_token)
            res.verified = True
        except DbWorkspaceError as exc:
            low = str(exc).lower()
            if "прав" in low or "permission" in low:
                res.notes.append("Проверка RESTORE VERIFYONLY недоступна: недостаточно прав (нужно право CREATE DATABASE).")
            else:
                res.verified = False
                res.ok = False
                res.error = f"Копия создана, но проверка не прошла: {exc}"
    view = local_view(path)
    if view is not None:
        res.file_visible = view.is_file()
        if res.file_visible:
            res.size_bytes = view.stat().st_size
        else:
            res.notes.append("Папка доступна с этого компьютера, но файл копии в ней не найден — возможно, это "
                             "другая папка с тем же именем на сервере.")
    else:
        res.notes.append("Папка находится на сервере и недоступна с этого компьютера: наличие файла подтверждено "
                         "проверкой на стороне сервера." if res.verified else
                         "Папка находится на сервере и недоступна с этого компьютера.")
    if res.size_bytes is None:
        try:
            for r in history(conn, o.database, 5):
                if r["path"] == path:
                    res.size_bytes = r["size"]
                    break
        except BackupError:
            pass
    return res


def _backup_hint(msg: str) -> str:
    low = msg.lower()
    if "operating system error 5" in low or "access is denied" in low or "отказано в доступе" in low:
        return msg + " — у службы SQL Server нет прав на запись в эту папку на сервере."
    if "operating system error 3" in low or "cannot find the path" in low:
        return msg + " — папка не существует на сервере (путь указывается относительно сервера)."
    if "compression" in low or "сжат" in low:
        return msg + " — сжатие резервных копий не поддерживается этой редакцией SQL Server."
    return msg


def history(conn: SqlConnection, database: str, limit: int = 30) -> list[dict]:
    try:
        res = conn.fetch_rows(HISTORY_SQL, (limit, database), safe_to_retry=True)
    except DbWorkspaceError as exc:
        raise BackupError(f"История из msdb недоступна: {exc}") from exc
    out = []
    for r in res.rows:
        out.append({"database": r[0], "started": r[1], "finished": r[2], "type": BACKUP_TYPES.get(str(r[3]), str(r[3])),
                    "copy_only": bool(r[4]), "size": int(r[5]) if r[5] is not None else None,
                    "compressed": int(r[6]) if r[6] is not None else None, "checksum": bool(r[7]), "path": r[8]})
    return out


# ------------------------------------------------------------------ очистка
_NAME_RE = r"^{db}_(\d{{8}}_\d{{6}})\.bak$"


def plan_cleanup(folder: Path, database: str, *, keep_last: int, older_than_days: int = 0,
                 now: dt.datetime | None = None) -> list[Path]:
    """Файлы копий этой базы (только с именами, созданными программой), которые можно удалить.

    Самые новые keep_last (не меньше 1) не удаляются никогда; при older_than_days > 0 удаляются лишь старше N дней.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise BackupError("Папка копий недоступна с этого компьютера — очистка невозможна.")
    keep_last = max(1, int(keep_last))
    rx = re.compile(_NAME_RE.format(db=re.escape(sanitize_filename(database, "db"))), re.I)
    found = []
    for p in folder.iterdir():
        m = rx.match(p.name)
        if m and p.is_file():
            try:
                stamp = dt.datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            found.append((stamp, p))
    found.sort(reverse=True)
    candidates = found[keep_last:]
    if older_than_days > 0:
        border = (now or dt.datetime.now()) - dt.timedelta(days=older_than_days)
        candidates = [(s, p) for s, p in candidates if s < border]
    return [p for _s, p in candidates]


def apply_cleanup(paths: list[Path]) -> tuple[list[Path], list[str]]:
    deleted, errors = [], []
    for p in paths:
        try:
            p.unlink()
            deleted.append(p)
        except OSError as exc:
            errors.append(f"{p.name}: {exc.strerror or exc}")
    return deleted, errors


# --------------------------------------------------------------- восстановление
def file_list(conn: SqlConnection, path: str) -> list[dict]:
    try:
        m = _exec(conn, FILELIST_SQL, [path])
    except DbWorkspaceError as exc:
        raise BackupError(f"Не удалось прочитать список файлов копии: {exc}") from exc
    cols = [c.lower() for c in m.columns]
    li, pi, ti = cols.index("logicalname"), cols.index("physicalname"), cols.index("type")
    return [{"logical": r[li], "physical": r[pi], "type": r[ti]} for r in m.rows]


def database_exists(conn: SqlConnection, name: str) -> bool:
    res = conn.fetch_rows("SELECT COUNT(*) FROM sys.databases WHERE name = ?", (name,), safe_to_retry=True)
    return bool(res.rows and res.rows[0][0])


def default_paths(conn: SqlConnection) -> tuple[str, str]:
    res = conn.fetch_rows(DEFAULT_PATHS_SQL, safe_to_retry=True)
    data, logp = (res.rows[0] if res.rows else (None, None))
    if not data:
        raise BackupError("Сервер не сообщил папку файлов данных по умолчанию (нужен SQL Server 2012+).")
    return str(data), str(logp or data)


def restore_as_new_sql(new_db: str, path: str, files: list[dict], data_dir: str, log_dir: str) -> tuple[str, list]:
    moves, params = [], [new_db, path]
    for i, f in enumerate(files):
        ext = PureWindowsPath(f["physical"]).suffix or (".ldf" if f["type"] == "L" else ".mdf")
        target_dir = log_dir if f["type"] == "L" else data_dir
        target = join_server_path(target_dir, sanitize_filename(f"{new_db}_{f['logical']}", "file") + ext)
        moves.append(f"MOVE @l{i} TO @p{i}")
        params += [f["logical"], target]
    declares = ["@db sysname = ?", "@path nvarchar(4000) = ?"]
    for i in range(len(files)):
        declares += [f"@l{i} nvarchar(256) = ?", f"@p{i} nvarchar(4000) = ?"]
    sql = (f"DECLARE {', '.join(declares)};\n"
           f"RESTORE DATABASE @db FROM DISK = @path WITH {', '.join(moves + ['CHECKSUM', 'RECOVERY', 'STATS = 10'])};")
    return sql, params


def restore_as_new(conn: SqlConnection, path: str, new_db: str, confirmation: str, cancel_token=None) -> list[str]:
    """Восстанавливает копию в НОВУЮ базу. Существующая база не затрагивается никогда.

    confirmation должен в точности совпадать с именем новой базы (защита от случайного запуска).
    """
    new_db = new_db.strip()
    if not new_db or len(new_db) > 128 or any(c in new_db for c in "[]\x00"):
        raise BackupError("Недопустимое имя новой базы данных.")
    if confirmation.strip() != new_db:
        raise BackupError("Подтверждение не совпадает с именем новой базы — восстановление не выполнено.")
    if new_db.lower() in SYSTEM_DBS:
        raise BackupError("Нельзя восстанавливать в системную базу.")
    if database_exists(conn, new_db):
        raise BackupError(f"База «{new_db}» уже существует. Программа восстанавливает только в новую базу; "
                          "замену существующей выполните вручную по инструкции.")
    files = file_list(conn, path)
    data_dir, log_dir = default_paths(conn)
    sql, params = restore_as_new_sql(new_db, path, files, data_dir, log_dir)
    conn.set_query_timeout(0)
    try:
        m = _exec(conn, sql, params, cancel_token)
    except DbWorkspaceError as exc:
        raise BackupError(f"Восстановление не выполнено: {exc}") from exc
    return m.messages


def restore_instructions(database: str, path: str) -> str:
    q = database.replace("]", "]]")
    p = path.replace("'", "''")
    return f"""ИНСТРУКЦИЯ ПО ВОССТАНОВЛЕНИЮ БАЗЫ «{database}»
Файл копии (путь на сервере): {path}

Перед восстановлением:
  1. Убедитесь, что копия цела: RESTORE VERIFYONLY FROM DISK = N'{p}' WITH CHECKSUM;
  2. Сделайте резервную копию текущей базы, если она существует и нужна.
  3. Предупредите пользователей: при замене базы все подключения к ней будут разорваны.

Вариант А (безопасный) — восстановить рядом под новым именем и сравнить данные:
  в DB Workspace Pro: «Автоматизация» → «Резервные копии» → «Восстановить как новую базу…».

Вариант Б — заменить существующую базу (выполняется вручную в SSMS, программа этого не делает):
  RESTORE FILELISTONLY FROM DISK = N'{p}';      -- логические имена файлов
  ALTER DATABASE [{q}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
  RESTORE DATABASE [{q}] FROM DISK = N'{p}' WITH REPLACE, CHECKSUM, RECOVERY, STATS = 10;
  ALTER DATABASE [{q}] SET MULTI_USER;

Если копия сделана с COPY_ONLY, она самостоятельная и не требует других копий для восстановления.
Требуются права: CREATE DATABASE (для новой базы) или роль sysadmin/dbcreator (для замены).
"""
