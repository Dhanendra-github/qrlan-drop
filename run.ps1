$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { python -m venv .venv }
    else { throw "Install Python 3.11 or newer from python.org, then try again." }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install dependencies." }
Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "app.py" -WorkingDirectory $ProjectDir
