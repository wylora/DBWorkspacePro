@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - задачи автоматизации
call "%~dp0_env.bat" || goto :fail_env
echo Задачи автоматизации: идентификатор, состояние, название, расписание, последний результат.
echo.
"%VENV_PY%" "%ROOT%\main.py" --list-tasks
echo.
pause
exit /b 0

:fail_env
pause
exit /b 1
