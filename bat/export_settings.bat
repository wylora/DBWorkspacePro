@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - экспорт настроек
call "%~dp0_env.bat" || goto :fail_env
set "DOCS="
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Environment]::GetFolderPath('MyDocuments')"`) do set "DOCS=%%d"
if not defined DOCS set "DOCS=%USERPROFILE%\Documents"
set "OUT=%DOCS%\DB Workspace Pro\Настройки"
if not exist "%OUT%" mkdir "%OUT%"
echo Экспорт подключений без паролей, шаблонов, избранного, SQL-сценариев и задач ...
echo.
"%VENV_PY%" "%ROOT%\main.py" --export-config "%OUT%"
if errorlevel 1 goto :failed
start "" explorer "%OUT%"
echo.
pause
exit /b 0

:failed
echo [ОШИБКА] Экспорт не выполнен - см. сообщение выше.
pause
exit /b 1

:fail_env
pause
exit /b 1
