$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "app.py" -WorkingDirectory $ProjectDir
