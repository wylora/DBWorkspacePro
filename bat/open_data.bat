@echo off
chcp 65001 >nul
setlocal
call "%~dp0_env.bat" --no-check
if defined DBWP_HOME set "DATA=%DBWP_HOME%"
if not defined DBWP_HOME set "DATA=%APPDATA%\DBWorkspacePro"
if not exist "%DATA%" goto :not_yet
start "" explorer "%DATA%"
exit /b 0

:not_yet
echo Папка данных ещё не создана: программа не запускалась.
echo Ожидаемое место: %DATA%
pause
exit /b 0
