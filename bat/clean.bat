@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - очистка
call "%~dp0_env.bat" --no-check
echo Будут удалены временные файлы разработки и сборки в папке проекта:
echo   __pycache__, .pytest_cache, build, dist
echo Окружения .venv / .venv-build и ДАННЫЕ ПРОГРАММЫ (%APPDATA%\DBWorkspacePro) НЕ затрагиваются.
echo.
choice /c YN /n /m "Удалить? [Y/N] "
if errorlevel 2 goto :cancelled
powershell -NoProfile -Command "Get-ChildItem -Path '%ROOT%' -Recurse -Directory -Force -Filter '__pycache__' | Where-Object { $_.FullName -notmatch '\\\.venv' } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
if exist "%ROOT%\.pytest_cache" rd /s /q "%ROOT%\.pytest_cache"
if exist "%ROOT%\build" rd /s /q "%ROOT%\build"
if exist "%ROOT%\dist" rd /s /q "%ROOT%\dist"
echo Готово.
pause
exit /b 0

:cancelled
echo Отменено.
pause
exit /b 0
