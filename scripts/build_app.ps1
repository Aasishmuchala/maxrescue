<#
.SYNOPSIS
    Build MaxRescue.exe — the Windows app, as a single file.

.EXAMPLE
    pwsh scripts\build_app.ps1
#>
[CmdletBinding()]
param([switch]$Clean)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if ($Clean) { Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue }

python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[app]" pyinstaller

# The suite must be green before anything is shipped. A build that skips this
# is how a broken .exe reaches someone else's machine.
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "tests failed - not building" }

python -m PyInstaller maxrescue.spec --noconfirm

$exe = Join-Path $repo "dist\MaxRescue.exe"
if (-not (Test-Path $exe)) { throw "the build produced no executable" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "==== built $exe ($size MB)"
Write-Host "Copy it anywhere. It needs no Python and no install."
Write-Host "3ds Max is only needed to RESCUE a scene - reading one needs nothing."
