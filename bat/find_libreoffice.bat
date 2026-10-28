@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - поиск LibreOffice
call "%~dp0_env.bat" || goto :fail_env
echo Поиск LibreOffice: путь из настроек, PATH, реестр Windows, папки программ, переносные версии.
echo.
"%VENV_PY%" "%ROOT%\main.py" --find-libreoffice
if errorlevel 1 goto :not_found
echo.
echo PDF и преобразования через LibreOffice доступны.
pause
exit /b 0

:not_found
echo.
echo Что сделать:
echo  1. Установите LibreOffice: https://www.libreoffice.org/download/
echo  2. Или в программе: Настройки - Путь к LibreOffice - кнопка Папка, и выберите папку LibreOffice.
echo     Подходит и основная папка установки, и вложенная папка program.
pause
exit /b 1

:fail_env
pause
exit /b 1
