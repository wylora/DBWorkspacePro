@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - быстрые тесты
call "%~dp0_env.bat" || goto :fail_env
echo Быстрые тесты - без полной проверки всех маршрутов конвертера, около минуты.
echo.
"%VENV_PY%" "%ROOT%\scripts\run_tests.py" --fast
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
