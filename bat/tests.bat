@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - все тесты
call "%~dp0_env.bat" || goto :fail_env
set "DBWP_SKIP_SLOW="
echo Все тесты. Если установлен LibreOffice, проверяются все маршруты конвертера - это несколько минут.
echo Интеграционные тесты с реальным сервером здесь пропускаются: для них bat\tests_integration.bat
echo.
"%VENV_PY%" "%ROOT%\scripts\run_tests.py"
if errorlevel 1 goto :failed
echo.
echo Все тесты прошли. Отчёт: %ROOT%\test-report.txt
pause
exit /b 0

:failed
echo.
echo [ВНИМАНИЕ] Есть ошибки. Пришлите файл отчёта: %ROOT%\test-report.txt
pause
exit /b 1

:fail_env
pause
exit /b 1
