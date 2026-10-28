@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - сборка .exe
call "%~dp0_env.bat" --no-check
where powershell >nul 2>nul
if errorlevel 1 goto :no_powershell
echo Сборка займёт несколько минут: отдельное окружение .venv-build, тесты, PyInstaller.
echo Если тесты не пройдут, сборка покажет отчёт и спросит, продолжать ли.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\packaging\build.ps1"
if errorlevel 1 goto :failed
echo.
echo Готово: %ROOT%\dist\DBWorkspacePro\DBWorkspacePro.exe
start "" explorer "%ROOT%\dist"
pause
exit /b 0

:failed
echo.
echo [ОШИБКА] Сборка не выполнена - см. сообщения выше.
pause
exit /b 1

:no_powershell
echo [ОШИБКА] Не найден PowerShell - он нужен для сборки.
pause
exit /b 1
