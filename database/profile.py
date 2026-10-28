"""Профиль подключения. Пароль в профиле НЕ хранится (см. security/credentials.py)."""
from __future__ import annotations

import dataclasses
import re
import uuid
from dataclasses import dataclass

AUTH_WINDOWS = "windows"
AUTH_SQL = "sql"
ENCRYPT_MODES = ("yes", "no", "strict")

_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_$#@ .-]{1,128}$")
_BAD_CHARS = re.compile(r"[\x00-\x1f]")


@dataclass
class ConnectionProfile:
    id: str
    name: str
    server: str
    instance: str = ""
    port: int | None = None
    database: str = ""
    auth: str = AUTH_WINDOWS
    username: str = ""
    driver: str = ""  # пусто = выбрать автоматически
    encrypt: str = "yes"
    trust_server_certificate: bool = False
    connect_timeout: int = 15
    query_timeout: int = 60  # 0 = без ограничения
    save_password: bool = False

    @staticmethod
    def new(name: str = "Новое подключение", server: str = "") -> "ConnectionProfile":
        return ConnectionProfile(id=uuid.uuid4().hex, name=name, server=server)

    def normalized(self) -> "ConnectionProfile":
        p = dataclasses.replace(
            self,
            name=self.name.strip(),
            server=self.server.strip(),
            instance=self.instance.strip(),
            database=self.database.strip(),
            username=self.username.strip(),
            driver=self.driver.strip(),
        )
        if "\\" in p.server and not p.instance:
            p.server, _, p.instance = p.server.partition("\\")
        if "," in p.server and p.port is None:
            host, _, port = p.server.partition(",")
            if port.strip().isdigit():
                p.server, p.port = host.strip(), int(port)
        return p

    def validate(self) -> list[str]:
        p = self.normalized()
        errs: list[str] = []
        if not p.name:
            errs.append("Укажите название подключения.")
        if not p.server:
            errs.append("Укажите имя сервера.")
        for label, val in (("сервера", p.server), ("базы данных", p.database), ("логина", p.username)):
            if _BAD_CHARS.search(val):
                errs.append(f"Имя {label} содержит недопустимые символы.")
        if p.instance and not _INSTANCE_RE.match(p.instance):
            errs.append("Недопустимое имя экземпляра.")
        if p.instance and p.port is not None:
            errs.append("Укажите либо экземпляр, либо порт (не оба сразу).")
        if p.port is not None and not (1 <= p.port <= 65535):
            errs.append("Порт должен быть от 1 до 65535.")
        if p.auth not in (AUTH_WINDOWS, AUTH_SQL):
            errs.append("Неизвестный тип аутентификации.")
        if p.auth == AUTH_SQL and not p.username:
            errs.append("Для аутентификации SQL Server укажите логин.")
        if p.encrypt not in ENCRYPT_MODES:
            errs.append("Недопустимый режим шифрования.")
        if not (1 <= p.connect_timeout <= 300):
            errs.append("Тайм-аут подключения: от 1 до 300 секунд.")
        if not (0 <= p.query_timeout <= 86400):
            errs.append("Тайм-аут запроса: от 0 до 86400 секунд (0 — без ограничения).")
        return errs

    def server_spec(self) -> str:
        p = self.normalized()
        if p.instance:
            return f"{p.server}\\{p.instance}"
        if p.port is not None:
            return f"{p.server},{p.port}"
        return p.server

    def display(self) -> str:
        db = f" / {self.database}" if self.database else ""
        return f"{self.server_spec()}{db}"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionProfile":
        names = {f.name for f in dataclasses.fields(cls)}
        clean = {k: v for k, v in data.items() if k in names}
        clean.setdefault("id", uuid.uuid4().hex)
        clean.setdefault("name", "Подключение")
        clean.setdefault("server", "")
        return cls(**clean)
