@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - диагностика
call "%~dp0_env.bat" || goto :fail_env
set "DESK="
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Environment]::GetFolderPath('Desktop')"`) do set "DESK=%%d"
if not defined DESK set "DESK=%USERPROFILE%\Desktop"
echo Сбор диагностического архива: проверка окружения, версии, настройки, последние ошибки, журналы.
echo.
"%VENV_PY%" "%ROOT%\main.py" --diagnostics "%DESK%"
if errorlevel 1 goto :failed
start "" explorer "%DESK%"
echo.
pause
exit /b 0

:failed
echo [ОШИБКА] Архив не создан - см. сообщение выше.
pause
exit /b 1

:fail_env
pause
exit /b 1
