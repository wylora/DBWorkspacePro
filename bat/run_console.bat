@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - запуск с консолью
call "%~dp0_env.bat" || goto :fail_env
echo Запуск с консолью: здесь будут видны сообщения, если программа не открывается.
echo.
"%VENV_PY%" "%ROOT%\main.py"
echo.
echo Программа завершилась, код %errorlevel%.
pause
exit /b 0

:fail_env
pause
exit /b 1
