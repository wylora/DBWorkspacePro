@echo off
rem Общие переменные для всех батников. Вызывается так: call "%~dp0_env.bat" [--no-check]
cd /d "%~dp0.."
set "ROOT=%CD%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "VENV_PYW=%ROOT%\.venv\Scripts\pythonw.exe"
if "%~1"=="--no-check" exit /b 0
if exist "%VENV_PY%" exit /b 0
echo.
echo Окружение программы не установлено.
echo Сначала запустите bat\install.bat (или пункт 1 в menu.bat).
echo.
exit /b 1
