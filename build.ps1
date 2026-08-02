$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python is not installed. Install Python 3.11 or newer from python.org, then run this script again."
}

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
& .\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name "QRLAN Drop" app.py

Write-Host ""
Write-Host "Built successfully: $ProjectDir\dist\QRLAN Drop.exe" -ForegroundColor Green
