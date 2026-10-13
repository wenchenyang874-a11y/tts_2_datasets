param([string]$Python = ".venv/Scripts/python.exe")
$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    if (-not (Test-Path -LiteralPath $Python)) { throw "Install requirements-build.txt in .venv first." }
    & $Python scripts/release_artifacts.py prepare
    if ($LASTEXITCODE -ne 0) { throw "Version resource generation failed." }
    & $Python scripts/build_icon.py
    if ($LASTEXITCODE -ne 0) { throw "Application icon build failed." }
    & $Python -m PyInstaller --noconfirm desktop.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
    & $Python scripts/package_notices.py "dist/TTS Dataset Studio"
    if ($LASTEXITCODE -ne 0) { throw "Dependency notice collection failed." }
    Write-Output "Portable folder: dist/TTS Dataset Studio"
} finally { Pop-Location }
