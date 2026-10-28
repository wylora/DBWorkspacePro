@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - установка
call "%~dp0_env.bat" --no-check
echo ============================================================
echo  DB Workspace Pro - установка и обновление окружения
echo ============================================================
echo.
set "PY="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_python
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :have_python
echo [ОШИБКА] Не найден Python 3.11 или новее.
echo Установите Python с https://www.python.org/downloads/
echo При установке отметьте пункт "Add python.exe to PATH" и запустите этот файл снова.
goto :failed

:have_python
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo Найден Python %PYVER% - команда: %PY%
if exist "%VENV_PY%" goto :have_venv
echo.
echo Создание виртуального окружения .venv ...
%PY% -m venv "%ROOT%\.venv"
if errorlevel 1 goto :venv_failed

:have_venv
echo.
echo Обновление pip ...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :pip_failed
echo.
echo Установка библиотек из requirements.txt ...
"%VENV_PY%" -m pip install --upgrade -r "%ROOT%\requirements.txt"
if errorlevel 1 goto :pip_failed
echo.
echo ------------------------------------------------------------
echo  Проверка окружения
echo ------------------------------------------------------------
"%VENV_PY%" "%ROOT%\scripts\check_env.py"
if errorlevel 1 goto :env_problems
echo.
echo Установка завершена. Запуск программы: bat\run.bat
goto :done

:env_problems
echo.
echo Библиотеки установлены, но проверка нашла проблемы - см. отчёт выше.
echo  - Драйвер ODBC для SQL Server: https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server
echo  - LibreOffice для PDF (необязательно): https://www.libreoffice.org/download/
goto :done

:venv_failed
echo [ОШИБКА] Не удалось создать виртуальное окружение .venv
goto :failed

:pip_failed
echo [ОШИБКА] Не удалось установить библиотеки.
echo Проверьте доступ в интернет. Если нужен прокси, задайте его так:
echo   set HTTPS_PROXY=http://прокси:порт
echo и запустите этот файл снова из той же командной строки.
goto :failed

:done
echo.
pause
exit /b 0

:failed
echo.
pause
exit /b 1
