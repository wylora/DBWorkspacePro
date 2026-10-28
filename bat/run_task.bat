@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - выполнить задачу
call "%~dp0_env.bat" || goto :fail_env
set "TASK_ID=%~1"
if defined TASK_ID goto :run
"%VENV_PY%" "%ROOT%\main.py" --list-tasks
echo.
set /p "TASK_ID=Идентификатор задачи из списка выше: "
if not defined TASK_ID goto :cancelled

:run
echo.
echo Выполнение задачи %TASK_ID% ...
"%VENV_PY%" "%ROOT%\main.py" --run-task %TASK_ID%
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" echo Результат: успешно.
if "%RC%"=="1" echo Результат: выполнено с замечаниями - подробности в истории задачи.
if "%RC%"=="2" echo Результат: ошибка - подробности в истории задачи и в журнале.
if "%~1"=="" pause
exit /b %RC%

:cancelled
echo Задача не выбрана.
pause
exit /b 0

:fail_env
pause
exit /b 1
