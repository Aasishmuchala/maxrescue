<#
.SYNOPSIS
    Rescue an oversized 3ds Max scene headlessly through 3dsmaxbatch.exe.

.DESCRIPTION
    Rebuilds the scene in batches under a memory ceiling, reducing each batch
    while only that batch is resident, and writes a NEW file. The source is
    never modified.

    The verdict is read from a RESULT FILE, never stdout — 3dsmaxbatch's stdout
    and exit codes do not reliably reflect what the script found.

.EXAMPLE
    pwsh scripts\run_rescue.ps1 -Target "D:\jobs\villa\villa.max" -CeilingGB 70

.EXAMPLE
    # Also convert bitmap loaders. NOT render-identical — gate it with a diff.
    pwsh scripts\run_rescue.ps1 -Target "D:\jobs\villa\villa.max" -ConvertBitmaps
#>
[CmdletBinding()]
param(
    [ValidateSet("2024", "2025", "2026", "2027")]
    [string]$MaxVersion = "2026",

    [Parameter(Mandatory = $true)]
    [string]$Target,

    [string]$Output = "",
    [double]$CeilingGB = 70,
    [switch]$ConvertBitmaps,
    [int]$TimeoutSec = 14400
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $repo "rescue-out"
$resultFile = Join-Path $outDir "_rescue_result.txt"
$listenerLog = Join-Path $outDir "_rescue_$MaxVersion.log"

if (-not (Test-Path $Target)) { throw "Scene not found: $Target" }
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Remove-Item $resultFile -ErrorAction SilentlyContinue   # never trust a stale verdict

$env:MAXRESCUE_REPO = $repo
$env:MAXRESCUE_RESULT = $resultFile
$env:MAXRESCUE_TARGET = (Resolve-Path $Target).Path
$env:MAXRESCUE_OUT = $outDir
$env:MAXRESCUE_CEILING_GB = "$CeilingGB"
$env:MAXRESCUE_OUTPUT = $Output
$env:MAXRESCUE_CONVERT_BITMAPS = if ($ConvertBitmaps) { "1" } else { "0" }

if ($ConvertBitmaps) {
    Write-Warning "Bitmap conversion is NOT render-identical. Verify with 'maxrescue verify' before shipping the result."
}

$batch = "C:\Program Files\Autodesk\3ds Max $MaxVersion\3dsmaxbatch.exe"
if (-not (Test-Path $batch)) { throw "3dsmaxbatch not found: $batch" }

foreach ($name in @("3dsmax", "3dsmaxbatch")) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "killing stale $name (pid $($_.Id))"
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
}

$script = Join-Path $PSScriptRoot "run_rescue.ms"
Write-Host "3ds Max $MaxVersion  ·  $($env:MAXRESCUE_TARGET)  ·  ceiling ${CeilingGB} GB"
Write-Host "listener log: $listenerLog"

$process = Start-Process -FilePath $batch `
    -ArgumentList @("`"$script`"", "-listenerlog", "`"$listenerLog`"", "-v", "5") `
    -PassThru -NoNewWindow

if (-not $process.WaitForExit($TimeoutSec * 1000)) {
    Write-Warning "timed out after $TimeoutSec s — killing"
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    exit 2
}

$known = @{
    -6   = "OUT OF MEMORY — lower -CeilingGB and re-run; the governor will resize the batches"
    -7   = "could not create a graphics device (headless/RDP display issue)"
    -8   = "license error"
    -110 = "could not start 3ds Max"
    -120 = "could not communicate with 3ds Max"
    -130 = "3ds Max reported an error (check the listener log)"
}
if ($known.ContainsKey($process.ExitCode)) {
    Write-Warning "3dsmaxbatch exit $($process.ExitCode): $($known[$process.ExitCode])"
}

if (-not (Test-Path $resultFile)) {
    Write-Error "no result file — the rescue did not reach the end. See $listenerLog"
    exit 1
}

$verdict = (Get-Content $resultFile -Raw).Trim()
Write-Host ""
Write-Host "==== $verdict"
Write-Host "report: $(Join-Path $outDir 'rescue_report.json')"

if ($verdict -like "RESCUE_OK*") { exit 0 } else { exit 1 }
