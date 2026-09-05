$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { python -m venv .venv }
    else { throw "Install Python 3.11 or newer from python.org, then try again." }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}

& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install build dependencies." }
& .\.venv\Scripts\python.exe tools\build_brand_assets.py
if ($LASTEXITCODE -ne 0) { throw "Could not prepare the brand assets." }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all tkinterdnd2 --add-data "assets;assets" --icon assets\app.ico --version-file version_info.txt --name "QRLAN Drop" app.py
if ($LASTEXITCODE -ne 0) { throw "Build failed. See the error above." }

Write-Host ""
Write-Host "Built successfully: $ProjectDir\dist\QRLAN Drop.exe" -ForegroundColor Green
