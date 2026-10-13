param([string]$Python = ".venv/Scripts/python.exe", [string]$Iscc = $env:INNO_SETUP_ISCC)
$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    if (-not $Iscc) {
        $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($command) { $Iscc = $command.Source }
        else {
            foreach ($candidate in @("${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe", "$env:LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe")) {
                if (Test-Path -LiteralPath $candidate) { $Iscc = $candidate; break }
            }
        }
    }
    if (-not $Iscc -or -not (Test-Path -LiteralPath $Iscc)) { throw "Supply -Iscc with Inno Setup 6.7.3 ISCC.exe path." }
    $Iscc = (Resolve-Path -LiteralPath $Iscc).Path
    $status = & git status --porcelain
    if ($LASTEXITCODE -ne 0 -or $status) { throw "Commit the release source first; build requires a clean Git tree." }
    $infoText = & $Python scripts/release_artifacts.py prepare
    if ($LASTEXITCODE -ne 0) { throw "Version preparation failed." }
    $info = $infoText | ConvertFrom-Json
    $output = Join-Path (Get-Location) "dist/releases/$($info.version)"
    if (Test-Path -LiteralPath $output) { throw "Version output already exists; archive the unpublished build before rebuilding the same version. Published assets must remain immutable." }
    & ./scripts/build_windows.ps1 -Python $Python
    New-Item -ItemType Directory -Path $output | Out-Null
    & $Iscc "/DAppVersion=$($info.version)" "/DNumericVersion=$($info.numeric)" "/DOutputDir=$output" "/DBundleDir=$((Resolve-Path -LiteralPath 'dist/TTS Dataset Studio').Path)" installer/windows.iss
    if ($LASTEXITCODE -ne 0) { throw "Installer compilation failed." }
    & $Python scripts/release_artifacts.py package
    if ($LASTEXITCODE -ne 0) { throw "Release packaging failed." }
} finally { Pop-Location }
