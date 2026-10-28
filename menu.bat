@echo off
chcp 65001 >nul
setlocal
title DB Workspace Pro - меню
set "B=%~dp0bat"

:menu
cls
echo  ============================================================
echo    DB Workspace Pro - меню
echo  ============================================================
echo    1  Установить или обновить окружение
echo    2  Запустить программу
echo    3  Запустить с консолью, если программа не открывается
echo    4  Проверить окружение
echo    5  Быстрые тесты
echo    6  Все тесты
echo    7  Интеграционные тесты с реальным сервером
echo    8  Собрать .exe
echo    9  Список задач автоматизации
echo    A  Выполнить задачу по идентификатору
echo    B  Экспорт настроек
echo    C  Импорт настроек
echo    D  Диагностический архив
echo    E  Открыть папку данных и журналов
echo    F  Очистить временные файлы сборки
echo    G  Найти LibreOffice - показать, где программа его ищет
echo    0  Выход
echo.
choice /c 123456789ABCDEFG0 /n /m "Выберите пункт: "
set "N=%errorlevel%"
if "%N%"=="17" exit /b 0
if "%N%"=="1" call "%B%\install.bat"
if "%N%"=="2" call "%B%\run.bat"
if "%N%"=="3" call "%B%\run_console.bat"
if "%N%"=="4" call "%B%\check_env.bat"
if "%N%"=="5" call "%B%\tests_fast.bat"
if "%N%"=="6" call "%B%\tests.bat"
if "%N%"=="7" call "%B%\tests_integration.bat"
if "%N%"=="8" call "%B%\build_exe.bat"
if "%N%"=="9" call "%B%\list_tasks.bat"
if "%N%"=="10" call "%B%\run_task.bat"
if "%N%"=="11" call "%B%\export_settings.bat"
if "%N%"=="12" call "%B%\import_settings.bat"
if "%N%"=="13" call "%B%\diagnostics.bat"
if "%N%"=="14" call "%B%\open_data.bat"
if "%N%"=="15" call "%B%\clean.bat"
if "%N%"=="16" call "%B%\find_libreoffice.bat"
goto :menu
