@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - импорт настроек
call "%~dp0_env.bat" || goto :fail_env
echo Импорт настроек. Можно просто перетащить файл .zip на этот батник.
echo Окно программы должно быть закрыто.
echo.
set "CFG=%~1"
if defined CFG goto :have_file
set /p "CFG=Путь к файлу настроек .zip: "
if not defined CFG goto :cancelled
set "CFG=%CFG:"=%"

:have_file
if not exist "%CFG%" goto :not_found
echo Файл: %CFG%
echo.
echo После импорта: задачи по расписанию будут ВЫКЛЮЧЕНЫ, подтверждения SQL-сценариев сняты,
echo пароли не переносятся - их нужно будет ввести при подключении.
echo.
set "OVER="
choice /c YN /n /m "Заменять существующие объекты с тем же идентификатором? [Y/N] "
if errorlevel 2 set "OVER=--no-overwrite"
choice /c YN /n /m "Выполнить импорт? [Y/N] "
if errorlevel 2 goto :cancelled
echo.
"%VENV_PY%" "%ROOT%\main.py" --import-config "%CFG%" %OVER%
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" echo Импорт выполнен.
if "%RC%"=="1" echo Импорт выполнен с замечаниями - см. список выше.
if "%RC%"=="2" echo [ОШИБКА] Импорт не выполнен - см. сообщение выше.
if "%RC%"=="3" echo Закройте окно программы и запустите импорт снова.
pause
exit /b %RC%

:not_found
echo [ОШИБКА] Файл не найден: %CFG%
pause
exit /b 1

:cancelled
echo Импорт отменён.
pause
exit /b 0

:fail_env
pause
exit /b 1
