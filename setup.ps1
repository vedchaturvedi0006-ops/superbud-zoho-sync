# One-time setup on Windows. Run in PowerShell from this folder:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python is not installed. Install it from https://www.python.org/downloads/ (tick 'Add to PATH') and run this again."
    exit 1
}
if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env - open it with:  notepad .env  and fill in the Zoho and Superbud values."
}
.\.venv\Scripts\python.exe -m unittest discover tests
Write-Host "`nSetup done. Next steps are in README.md (auth, check, dry run, schedule)."
