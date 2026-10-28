# Сборка DB Workspace Pro в папку dist\DBWorkspacePro (Windows, PowerShell 5.1+).
# Запуск: bat\build_exe.bat  или  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-SkipTests]
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONUTF8 = "1"

function Step([string]$title, [scriptblock]$action) {
    Write-Host ""
    Write-Host "== $title" -ForegroundColor Cyan
    & $action
    if ($LASTEXITCODE -ne 0) { throw "Шаг не выполнен: $title (код $LASTEXITCODE)" }
}

$venvPy = ".\.venv-build\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Step "Создание окружения сборки (py -3)" { py -3 -m venv .venv-build }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        Step "Создание окружения сборки (python)" { python -m venv .venv-build }
    } else {
        throw "Не найден Python 3.11+. Установите его с https://www.python.org/downloads/"
    }
}

Step "Обновление pip" { & $venvPy -m pip install --upgrade pip }
Step "Установка зависимостей и PyInstaller" { & $venvPy -m pip install --upgrade -r requirements.txt pyinstaller }

if ($SkipTests) {
    Write-Host ""
    Write-Host "== Тесты пропущены по ключу -SkipTests" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "== Тесты (отчёт: build-test-report.txt)" -ForegroundColor Cyan
    & $venvPy scripts\run_tests.py --report build-test-report.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Тесты не прошли. Отчёт: $((Resolve-Path build-test-report.txt).Path)" -ForegroundColor Yellow
        $answer = Read-Host "Собрать .exe несмотря на это? Y - продолжить, Enter - остановить"
        if ($answer -notin @("Y", "y", "Д", "д")) { throw "Сборка остановлена: тесты не прошли." }
    }
}

Step "Значок программы" { & $venvPy packaging\make_icon.py }
Step "Сведения о версии" { & $venvPy packaging\make_version_info.py }
Step "PyInstaller" { & $venvPy -m PyInstaller --noconfirm --clean packaging\DBWorkspacePro.spec }

Copy-Item README.md dist\DBWorkspacePro\ -Force
$ver = (& $venvPy -c "from version import __version__; print(__version__)").Trim()
$zip = "dist\DBWorkspacePro-$ver-win64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path dist\DBWorkspacePro -DestinationPath $zip
Write-Host ""
Write-Host "Готово: dist\DBWorkspacePro\DBWorkspacePro.exe и $zip" -ForegroundColor Green
Write-Host "Проверка без окна: dist\DBWorkspacePro\DBWorkspacePro.exe --list-tasks (вывод в журнале cli-output.txt)"
