<#
.SYNOPSIS
    Run the MaxRescue P0 spikes headlessly through 3dsmaxbatch.exe.

.DESCRIPTION
    The verdict is read from a RESULT FILE, never from stdout — 3dsmaxbatch's
    stdout and exit codes are not reliable indicators of what the script found.

    Zombie 3dsmax processes are killed first: a sibling project exhausted this
    machine's pagefile after ~12 back-to-back launches and Max then began
    crashing at boot, which looked like a plugin bug and was not.

.EXAMPLE
    pwsh scripts\run_spikes.ps1 -Target "D:\jobs\villa\villa.max"

.EXAMPLE
    # On the 256 GB box, where the scene actually opens, also run S3:
    $env:MAXRESCUE_ALLOW_FULL_OPEN = "1"
    pwsh scripts\run_spikes.ps1 -Target "D:\jobs\villa\villa.max" -TimeoutSec 3600
#>
[CmdletBinding()]
param(
    [ValidateSet("2024", "2025", "2026", "2027")]
    [string]$MaxVersion = "2026",

    [Parameter(Mandatory = $true)]
    [string]$Target,

    [int]$TimeoutSec = 1800
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $repo "spike-out"
$resultFile = Join-Path $outDir "_spike_result.txt"
$listenerLog = Join-Path $outDir "_batch_$MaxVersion.log"

if (-not (Test-Path $Target)) { throw "Target scene not found: $Target" }
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Remove-Item $resultFile -ErrorAction SilentlyContinue   # never trust a stale verdict

$env:MAXRESCUE_REPO = $repo
$env:MAXRESCUE_RESULT = $resultFile
$env:MAXRESCUE_TARGET = (Resolve-Path $Target).Path
$env:MAXRESCUE_OUT = $outDir

$batch = "C:\Program Files\Autodesk\3ds Max $MaxVersion\3dsmaxbatch.exe"
if (-not (Test-Path $batch)) { throw "3dsmaxbatch not found: $batch" }

# Stale BATCH processes only. Never 3dsmax.exe: an artist may have a scene open
# with hours of unsaved work, and this script has no way to tell a zombie from a
# colleague. Killing it would be the most expensive thing this tool ever did.
Get-Process -Name "3dsmaxbatch" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "killing stale 3dsmaxbatch (pid $($_.Id))"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
$interactive = @(Get-Process -Name "3dsmax" -ErrorAction SilentlyContinue)
if ($interactive.Count -gt 0) {
    Write-Warning "$($interactive.Count) interactive 3ds Max process(es) are running. Not touching them - close them yourself if this run needs the memory."
}

$script = Join-Path $PSScriptRoot "run_spikes.ms"
Write-Host "3ds Max $MaxVersion  ·  target $($env:MAXRESCUE_TARGET)"
Write-Host "listener log: $listenerLog"

$process = Start-Process -FilePath $batch `
    -ArgumentList @("`"$script`"", "-listenerlog", "`"$listenerLog`"", "-v", "5") `
    -PassThru -NoNewWindow

if (-not $process.WaitForExit($TimeoutSec * 1000)) {
    Write-Warning "timed out after $TimeoutSec s — killing"
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    exit 2
}

# Documented 3dsmaxbatch exit codes worth naming, so a failure is diagnosable
# rather than a bare number.
$known = @{
    -6   = "OUT OF MEMORY — the machine could not hold what the script asked for"
    -7   = "could not create a graphics device (headless/RDP display issue)"
    -8   = "license error"
    -110 = "could not start 3ds Max"
    -120 = "could not communicate with 3ds Max"
    -130 = "3ds Max reported an error (check the listener log; stdout logging alone can cause this)"
}
if ($known.ContainsKey($process.ExitCode)) {
    Write-Warning "3dsmaxbatch exit $($process.ExitCode): $($known[$process.ExitCode])"
}

if (-not (Test-Path $resultFile)) {
    Write-Error "no result file — the spikes did not reach the end. See $listenerLog"
    exit 1
}

$verdict = (Get-Content $resultFile -Raw).Trim()
Write-Host ""
Write-Host "==== $verdict"
Write-Host "report: $(Join-Path $outDir 'spike_report.json')"

if ($verdict -like "SPIKES_OK*") { exit 0 } else { exit 1 }
