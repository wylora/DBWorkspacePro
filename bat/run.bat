@echo off
chcp 65001 >nul
setlocal
call "%~dp0_env.bat" || goto :fail_env
if not exist "%VENV_PYW%" goto :no_pythonw
start "" "%VENV_PYW%" "%ROOT%\main.py"
exit /b 0

:no_pythonw
start "" "%VENV_PY%" "%ROOT%\main.py"
exit /b 0

:fail_env
pause
exit /b 1
