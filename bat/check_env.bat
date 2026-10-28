@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - проверка окружения
call "%~dp0_env.bat" --no-check
set "PYRUN=%VENV_PY%"
if exist "%VENV_PY%" goto :run
set "PYRUN="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYRUN=py -3"
if defined PYRUN goto :run_system
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYRUN=python"
if defined PYRUN goto :run_system
echo Python не найден. Установите Python 3.11+ и запустите bat\install.bat
pause
exit /b 1

:run_system
echo Окружение .venv ещё не установлено - проверка системным Python.
echo.
%PYRUN% "%ROOT%\scripts\check_env.py"
goto :done

:run
"%PYRUN%" "%ROOT%\scripts\check_env.py"

:done
echo.
pause
exit /b 0
