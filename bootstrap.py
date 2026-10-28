"""Сборка сервисов приложения (без Qt — чтобы ядро проверялось тестами)."""
from __future__ import annotations

from dataclasses import dataclass

from database.manager import ConnectionManager
from database.profile_service import ProfileService
from database.schema_cache import SchemaCache
from security.credentials import CredentialStore, default_store
from storage.db import AppDatabase
from storage.history import OperationLog
from storage.profiles import ProfileRepository
from storage.queries import FavoriteQueries, QueryHistory
from storage.settings import SettingsStore
from utils.paths import AppPaths
from utils.tasks import TaskInfo, TaskManager, TaskState


@dataclass
class AppServices:
    paths: AppPaths
    db: AppDatabase
    settings: SettingsStore
    oplog: OperationLog
    credentials: CredentialStore
    manager: ConnectionManager
    profiles: ProfileService
    tasks: TaskManager
    history: QueryHistory
    favorites: FavoriteQueries
    schema_cache: SchemaCache

    def shutdown(self) -> None:
        self.tasks.shutdown(cancel_running=True, timeout=5.0)
        self.manager.close_all()
        self.db.close()


def create_services(home=None, credentials: CredentialStore | None = None,
                    connection_factory=None) -> AppServices:
    paths = AppPaths(home).ensure()
    db = AppDatabase(paths.db_file)
    settings = SettingsStore(db)
    oplog = OperationLog(db)
    creds = credentials or default_store()
    manager = ConnectionManager(settings.get("max_connections"), connection_factory)
    profiles = ProfileService(ProfileRepository(db), creds, manager, oplog)

    def record(task: TaskInfo) -> None:
        status = {TaskState.SUCCEEDED: "ok", TaskState.FAILED: "error",
                  TaskState.CANCELLED: "warning"}.get(task.state, "info")
        msg = task.error or ("отменена" if task.state is TaskState.CANCELLED else "")
        oplog.add("task", task.name, status, msg, task.duration_ms)

    tasks = TaskManager(settings.get("max_parallel_tasks"), recorder=record)
    history = QueryHistory(db, settings.get("query_history_limit"))
    return AppServices(paths, db, settings, oplog, creds, manager, profiles, tasks,
                       history, FavoriteQueries(db), SchemaCache())
