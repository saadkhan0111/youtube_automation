# Starts the Shorts Publisher (backend + built dashboard) on http://localhost:8000
# Usage:  powershell -ExecutionPolicy Bypass -File .\start.ps1        (add -Rebuild to rebuild the dashboard)
param([switch]$Rebuild)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$py = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating Python virtual environment..."
    python -m venv (Join-Path $backend ".venv")
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $backend "requirements.txt")
}
if ($Rebuild -or -not (Test-Path (Join-Path $frontend "dist\index.html"))) {
    Write-Host "Building dashboard..."
    Push-Location $frontend
    if (-not (Test-Path "node_modules")) { npm install --no-audit --no-fund }
    npm run build
    Pop-Location
}
Push-Location $backend
Write-Host "Shorts Publisher running at http://localhost:8000  (Ctrl+C to stop)"
& $py -m app.main
Pop-Location
