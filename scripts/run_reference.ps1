<#
.SYNOPSIS
    Render a reference frame, to prove later that a rescue changed nothing.

.DESCRIPTION
    Run this on the machine where the scene STILL OPENS — the reference has to
    come from the original, and the original is the file that does not fit.

    It also computes and saves the light cache. Both this render and the
    post-rescue one must load that same cache, or they differ for reasons that
    have nothing to do with the rescue and the comparison is worthless.

.EXAMPLE
    pwsh scripts\run_reference.ps1 -Target "D:\jobs\villa\villa.max" -Camera "Cam_Hero"
#>
[CmdletBinding()]
param(
    [ValidateSet("2024", "2025", "2026", "2027")]
    [string]$MaxVersion = "2026",

    [Parameter(Mandatory = $true)]
    [string]$Target,

    [string]$Camera = "",
    [int]$Width = 1280,
    [int]$Height = 720,
    [int]$TimeoutSec = 7200
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $repo "verify-out"
$resultFile = Join-Path $outDir "_reference_result.txt"
$listenerLog = Join-Path $outDir "_reference_$MaxVersion.log"

if (-not (Test-Path $Target)) { throw "Scene not found: $Target" }
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Remove-Item $resultFile -ErrorAction SilentlyContinue

$env:MAXRESCUE_REPO = $repo
$env:MAXRESCUE_RESULT = $resultFile
$env:MAXRESCUE_TARGET = (Resolve-Path $Target).Path
$env:MAXRESCUE_OUT = $outDir
$env:MAXRESCUE_REFERENCE = Join-Path $outDir "reference.exr"
$env:MAXRESCUE_LIGHTCACHE = Join-Path $outDir "reference.vrlmap"
$env:MAXRESCUE_CAMERA = $Camera
$env:MAXRESCUE_WIDTH = "$Width"
$env:MAXRESCUE_HEIGHT = "$Height"

$batch = "C:\Program Files\Autodesk\3ds Max $MaxVersion\3dsmaxbatch.exe"
if (-not (Test-Path $batch)) { throw "3dsmaxbatch not found: $batch" }

Get-Process -Name "3dsmaxbatch" -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

$script = Join-Path $PSScriptRoot "run_reference.ms"
Write-Host "reference render: $($env:MAXRESCUE_TARGET) -> $($env:MAXRESCUE_REFERENCE)"

$process = Start-Process -FilePath $batch `
    -ArgumentList @("`"$script`"", "-listenerlog", "`"$listenerLog`"", "-v", "5") `
    -PassThru -NoNewWindow

if (-not $process.WaitForExit($TimeoutSec * 1000)) {
    Write-Warning "timed out after $TimeoutSec s - killing"
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    exit 2
}

$known = @{
    -6 = "OUT OF MEMORY - the reference must be rendered where the scene fits"
    -7 = "could not create a graphics device (headless/RDP display issue)"
    -8 = "license error"
    -130 = "3ds Max reported an error (check the listener log)"
}
if ($known.ContainsKey($process.ExitCode)) {
    Write-Warning "3dsmaxbatch exit $($process.ExitCode): $($known[$process.ExitCode])"
}

if (-not (Test-Path $resultFile)) {
    Write-Error "no result file. See $listenerLog"
    exit 1
}

$verdict = (Get-Content $resultFile -Raw).Trim()
Write-Host ""
Write-Host "==== $verdict"
Write-Host ""
Write-Host "Now rescue with the same settings to prove nothing changed:"
Write-Host "  `$env:MAXRESCUE_REFERENCE  = `"$($env:MAXRESCUE_REFERENCE)`""
Write-Host "  `$env:MAXRESCUE_LIGHTCACHE = `"$($env:MAXRESCUE_LIGHTCACHE)`""
Write-Host "  `$env:MAXRESCUE_CAMERA     = `"$Camera`""
Write-Host "  pwsh scripts\run_rescue.ps1 -Target `"$Target`""

if ($verdict -like "REFERENCE_OK*") { exit 0 } else { exit 1 }
