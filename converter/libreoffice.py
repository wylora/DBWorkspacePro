"""Конвертация документов через LibreOffice в headless-режиме.

* на каждый запуск — отдельный временный профиль (нет конфликтов блокировок, можно запускать параллельно);
* отмена и тайм-аут завершают дерево процессов LibreOffice;
* результат проверяется (файл есть, не пустой, для PDF — сигнатура %PDF).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("dbworkspace.converter")

INSTALL_HINT = ("LibreOffice не найден. Установите его с https://www.libreoffice.org/download/ (бесплатно) "
                "и перезапустите приложение либо укажите путь к soffice в настройках.")


class ConversionError(Exception):
    pass


class ConversionCancelled(ConversionError):
    pass


@dataclass
class SofficeLookup:
    """Результат поиска LibreOffice: путь, где найден, и что проверялось (для диагностики)."""
    path: str | None
    source: str = ""
    checked: list[str] = field(default_factory=list)
    note: str = ""


_GUI_EXES = ("swriter", "scalc", "simpress", "sdraw", "sbase", "smath", "soffice.bin", "libreoffice")


def _exe_names(windows: bool) -> tuple[str, ...]:
    return ("soffice.exe", "soffice.com") if windows else ("soffice", "libreoffice")


def _in_dir(d: Path, windows: bool) -> str | None:
    """soffice в папке: сама папка program, корень установки, корень переносной версии или пакет macOS."""
    for sub in ("", "program", "App/libreoffice/program", "LibreOffice/program", "Contents/MacOS"):
        base = d / sub if sub else d
        for name in _exe_names(windows):
            if (base / name).is_file():
                return str(base / name)
    return None


def _from_custom(custom: str, windows: bool) -> str | None:
    p = Path(custom.strip().strip('"'))
    if p.is_dir():
        return _in_dir(p, windows)
    if p.is_file():
        stem = p.name.lower().removesuffix(".exe").removesuffix(".com")
        if stem in _GUI_EXES or p.name.lower() == "soffice.bin":     # выбран другой файл из папки LibreOffice
            return _in_dir(p.parent, windows) or str(p)
        return str(p)
    return None


def _registry_candidates() -> list[str]:
    """Пути из реестра Windows: App Paths, UNO\\InstallPath, LibreOffice\\<версия>\\Path."""
    try:
        import winreg
    except ImportError:
        return []
    out: list[str] = []

    def read(hive, key, value=""):
        for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                    v, _ = winreg.QueryValueEx(k, value)
                    if v:
                        out.append(str(v))
            except OSError:
                continue

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        read(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\soffice.exe")
        read(hive, r"SOFTWARE\LibreOffice\UNO\InstallPath")
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\LibreOffice\LibreOffice") as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    read(hive, rf"SOFTWARE\LibreOffice\LibreOffice\{winreg.EnumKey(k, i)}", "Path")
        except OSError:
            pass
    return out


def _windows_dirs(env) -> list[tuple[str, Path]]:
    bases: list[Path] = []
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if env.get(var):
            bases.append(Path(env[var]))
    bases += [Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")]
    if env.get("LOCALAPPDATA"):
        bases.append(Path(env["LOCALAPPDATA"]) / "Programs")
    seen, out = set(), []
    for b in bases:
        if str(b).lower() not in seen:
            seen.add(str(b).lower())
            out.append(("папка программ", b))
    home = Path(env["USERPROFILE"]) if env.get("USERPROFILE") else None
    if home:
        out.append(("Scoop", home / "scoop" / "apps" / "libreoffice" / "current"))
        out.append(("PortableApps", home / "PortableApps" / "LibreOfficePortable"))
    for drive in "CDEF":
        out.append(("PortableApps", Path(f"{drive}:\\PortableApps\\LibreOfficePortable")))
    return out


def locate_soffice(custom: str = "", *, env=None, platform: str | None = None, which=shutil.which,
                   registry=_registry_candidates) -> SofficeLookup:
    """Ищет LibreOffice: путь из настроек (файл или папка), PATH, реестр Windows, стандартные и переносные установки.

    Если путь из настроек не подходит, поиск продолжается автоматически, а в note объясняется, что произошло.
    """
    env = os.environ if env is None else env
    windows = (platform or sys.platform) == "win32"
    res = SofficeLookup(None)
    if custom and custom.strip():
        res.checked.append(f"путь из настроек: {custom}")
        found = _from_custom(custom, windows)
        if found:
            res.path, res.source = found, "путь из настроек"
            return res
        res.note = (f"Путь из настроек не подходит ({custom}): там нет soffice. "
                    "Можно указать сам файл soffice.exe или папку LibreOffice.")
    res.checked.append("переменная PATH")
    for name in ("soffice", "libreoffice"):
        found = which(name)
        if found:
            res.path, res.source = found, "PATH"
            return res
    if windows:
        cands = registry() or []
        res.checked.append("реестр Windows" + (f" ({len(cands)} записей)" if cands else " (записей нет)"))
        for c in cands:
            p = Path(c.strip().strip('"'))
            found = _from_custom(str(p), True) if p.exists() else None
            if found:
                res.path, res.source = found, "реестр Windows"
                return res
        for label, base in _windows_dirs(env):
            if label == "папка программ":
                res.checked.append(str(base / "LibreOffice*"))
                for d in sorted(base.glob("LibreOffice*"), reverse=True) if base.is_dir() else []:
                    found = _in_dir(d, True)
                    if found:
                        res.path, res.source = found, label
                        return res
            else:
                res.checked.append(str(base))
                if base.is_dir():
                    found = _in_dir(base, True)
                    if found:
                        res.path, res.source = found, label
                        return res
    else:
        for cand in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", "/usr/lib/libreoffice/program/soffice",
                     "/opt/libreoffice/program/soffice", "/snap/bin/libreoffice"):
            res.checked.append(cand)
            if Path(cand).is_file():
                res.path, res.source = cand, "стандартная папка"
                return res
        for d in sorted(Path("/opt").glob("libreoffice*"), reverse=True):
            found = _in_dir(d, False)
            if found:
                res.path, res.source = found, "стандартная папка"
                return res
    return res


def find_soffice(custom: str = "") -> str | None:
    return locate_soffice(custom).path


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=0x08000000)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def verify_output(path: Path, ext: str) -> None:
    if not path.is_file():
        raise ConversionError("LibreOffice не создал выходной файл.")
    size = path.stat().st_size
    if size == 0:
        raise ConversionError("LibreOffice создал пустой файл.")
    if ext.lower() == "pdf":
        with open(path, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise ConversionError("Получен повреждённый PDF (нет сигнатуры %PDF).")


def convert(src: str | os.PathLike, target_ext: str, out_dir: str | os.PathLike, *, soffice: str | None = None,
            timeout: float = 180.0, cancel_token=None, infilter: str | None = None) -> Path:
    """Конвертирует src в формат target_ext (pdf, docx, odt, rtf, txt, html, xlsx, csv …). Возвращает путь результата."""
    exe = soffice or find_soffice()
    if not exe:
        raise ConversionError(INSTALL_HINT)
    src, out_dir = Path(src), Path(out_dir)
    if not src.is_file():
        raise ConversionError(f"Исходный файл не найден: {src}")
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="dbwp-lo-"))
    expected = out_dir / (src.stem + "." + target_ext.split(":")[0])
    if expected.exists():
        expected.unlink()
    args = [exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", "--nolockcheck",
            "--nodefault", "--nofirststartwizard"]
    if infilter:
        args.append(f"--infilter={infilter}")
    args += ["--convert-to", target_ext, "--outdir", str(out_dir), str(src)]
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)
    except OSError as exc:
        shutil.rmtree(profile, ignore_errors=True)
        raise ConversionError(f"Не удалось запустить LibreOffice: {exc.strerror or exc}") from exc
    started = time.monotonic()
    try:
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel_token is not None and cancel_token.is_cancelled:
                _kill_tree(proc)
                proc.wait(timeout=10)
                _cleanup_partial(expected)
                raise ConversionCancelled("Преобразование отменено.")
            if time.monotonic() - started > timeout:
                _kill_tree(proc)
                proc.wait(timeout=10)
                _cleanup_partial(expected)
                raise ConversionError(f"LibreOffice не завершил работу за {int(timeout)} с (тайм-аут).")
        err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip() if proc.stderr else ""
        if proc.returncode != 0:
            raise ConversionError(f"LibreOffice завершился с ошибкой ({proc.returncode}). {err[:300]}")
        verify_output(expected, target_ext.split(":")[0])
        return expected
    finally:
        for stream in (proc.stdout, proc.stderr):
            try:
                stream and stream.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(profile, ignore_errors=True)


def _cleanup_partial(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
