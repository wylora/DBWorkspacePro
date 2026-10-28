"""Межпроцессные блокировки на файлах (ОС снимает блокировку сама, если процесс завершился аварийно)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class ProcessLock:
    """Неблокирующая блокировка: try_acquire() возвращает False, если её держит другой процесс."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._fh = None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode())
            fh.flush()
        except OSError:
            pass
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    @property
    def held(self) -> bool:
        return self._fh is not None

    def __enter__(self):
        if not self.try_acquire():
            raise BlockingIOError(f"Блокировка занята другим процессом: {self.path.name}")
        return self

    def __exit__(self, *exc):
        self.release()
