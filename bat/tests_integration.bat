@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - интеграционные тесты
call "%~dp0_env.bat" || goto :fail_env
echo ============================================================
echo  Интеграционные тесты с РЕАЛЬНЫМ SQL Server
echo ============================================================
echo Тесты только читают данные. Используйте тестовый сервер или тестовую базу.
echo Резервное копирование проверяется отдельно и только с вашего согласия.
echo.
set "DBWP_TEST_SERVER="
set /p "DBWP_TEST_SERVER=Сервер, например SRV01 или SRV01\SQLEXPRESS: "
if not defined DBWP_TEST_SERVER goto :cancelled
set "DBWP_TEST_DATABASE="
set /p "DBWP_TEST_DATABASE=База данных [master]: "
if not defined DBWP_TEST_DATABASE set "DBWP_TEST_DATABASE=master"
choice /c WS /n /m "Вход: W - учётная запись Windows, S - логин SQL Server [W/S] "
if errorlevel 2 goto :sql_auth
set "DBWP_TEST_AUTH=windows"
goto :certificate

:sql_auth
set "DBWP_TEST_AUTH=sql"
set "DBWP_TEST_USER="
set /p "DBWP_TEST_USER=Логин SQL Server: "
set "DBWP_TEST_PASSWORD="
set "DBWP_TEST_PASSWORD_PROMPT=1"
echo Пароль будет запрошен скрыто при запуске тестов.

:certificate
set "DBWP_TEST_TRUST_CERT="
choice /c YN /n /m "Доверять сертификату сервера, например самоподписанному? [Y/N] "
if errorlevel 2 goto :backup
set "DBWP_TEST_TRUST_CERT=1"

:backup
set "DBWP_TEST_BACKUP_FOLDER="
echo.
echo Проверка резервного копирования создаст файл .bak с COPY_ONLY на сервере. Данные базы не меняются.
choice /c YN /n /m "Проверить резервное копирование? [Y/N] "
if errorlevel 2 goto :run
set /p "DBWP_TEST_BACKUP_FOLDER=Папка НА СЕРВЕРЕ для файла .bak, например D:\Backup: "

:run
echo.
"%VENV_PY%" "%ROOT%\scripts\run_tests.py" tests.integration.test_real_server --report "%ROOT%\test-report-integration.txt"
set "RC=%errorlevel%"
set "DBWP_TEST_PASSWORD="
echo.
if "%RC%"=="0" echo Интеграционные тесты прошли.
if not "%RC%"=="0" echo [ВНИМАНИЕ] Есть ошибки. Отчёт: %ROOT%\test-report-integration.txt
pause
exit /b %RC%

:cancelled
echo Сервер не указан - тесты не запускались.
pause
exit /b 0

:fail_env
pause
exit /b 1
