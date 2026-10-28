"""Безопасная работа с файлами: имена, атомарная запись, свободное место."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class InsufficientDiskSpaceError(OSError):
    pass


def sanitize_filename(name: str, default: str = "file", max_len: int = 150) -> str:
    """Делает имя файла допустимым для Windows (без путей и запрещённых символов)."""
    name = _INVALID.sub("_", name or "").strip().rstrip(". ")
    if not name:
        name = default
    stem, dot, ext = name.partition(".")
    if stem.upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_len:
        base, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = base[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def is_within(base: str | os.PathLike, path: str | os.PathLike) -> bool:
    """True, если path находится внутри base (после разрешения '..' и ссылок)."""
    base_r = Path(base).resolve()
    path_r = Path(path).resolve()
    try:
        path_r.relative_to(base_r)
        return True
    except ValueError:
        return False


def ensure_free_space(path: str | os.PathLike, required_bytes: int, margin_bytes: int = 50 * 1024 * 1024) -> int:
    """Проверяет свободное место на диске, где лежит path. Возвращает свободные байты."""
    p = Path(path)
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < required_bytes + margin_bytes:
        raise InsufficientDiskSpaceError(
            f"Недостаточно места на диске: свободно {free / 1_048_576:.0f} МБ, "
            f"требуется около {(required_bytes + margin_bytes) / 1_048_576:.0f} МБ."
        )
    return free


def atomic_write_bytes(path: str | os.PathLike, data: bytes) -> Path:
    """Пишет файл через временный файл в той же папке и атомарно переименовывает."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def atomic_write_text(path: str | os.PathLike, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


MAX_SQL_FILE_BYTES = 10 * 1024 * 1024


def read_text_auto(path: str | os.PathLike, max_bytes: int = MAX_SQL_FILE_BYTES) -> tuple[str, str]:
    """Читает текстовый файл (SQL) с определением кодировки. Возвращает (текст, кодировка).

    UTF-8 (с BOM или без) и UTF-16 с BOM определяются надёжно; иначе используется cp1251
    (типично для старых русскоязычных скриптов) — такая догадка может быть неточной.
    """
    p = Path(path)
    size = p.stat().st_size
    if size > max_bytes:
        raise ValueError(f"Файл слишком большой ({size / 1_048_576:.1f} МБ, максимум {max_bytes // 1_048_576} МБ).")
    data = p.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        text, enc = data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text, enc = data.decode("utf-16", errors="replace"), "utf-16"
    else:
        try:
            text, enc = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, enc = data.decode("cp1251", errors="replace"), "cp1251"
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc


def write_sql_file(path: str | os.PathLike, text: str) -> Path:
    """Атомарно сохраняет скрипт: UTF-8 с BOM (так его верно открывает SSMS) и переводами строк Windows."""
    return atomic_write_bytes(path, text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig"))


def atomic_copy(src: str | os.PathLike, dest: str | os.PathLike) -> Path:
    """Копирует файл в dest через временное имя в папке назначения и атомарно переименовывает
    (работает и между разными дисками, в отличие от os.replace)."""
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
