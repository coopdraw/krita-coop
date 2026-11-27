# Runs tests/krita_live_gui.py inside the co-op build of Krita (real application, real brush strokes).
#
#   powershell -ExecutionPolicy Bypass -File tests\run_live_gui.ps1
#
# Krita is started with its own resource folder (default brushes). A small plugin that runs the test is
# put into the build's plugin folder for the duration of the test and enabled through
# KRITA_FORCE_PYTHON_PLUGINS, so your Krita settings are not changed.
# A Krita window opens for the duration of the test (about a minute) and is closed again.
param(
    # the build tree: %KRITA_DEV%, or the folder krita-dev in this repository
    [string]$KritaDev = $(if ($env:KRITA_DEV) { $env:KRITA_DEV } else { Join-Path (Split-Path -Parent $PSScriptRoot) "krita-dev" }),
    [int]$TimeoutSec = 600
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$krita = Join-Path $KritaDev "_install\bin\krita.exe"
if (-not (Test-Path $krita)) { throw "No co-op build of Krita at $krita" }

$res = Join-Path $KritaDev "testres"
New-Item -ItemType Directory -Force $res | Out-Null
# (plugins in a --resource-location folder are listed but can't be imported, so it goes next to the built-in ones)
$pykrita = Join-Path $KritaDev "_install\share\krita\pykrita"
$plug = Join-Path $pykrita "coop_selftest"
New-Item -ItemType Directory -Force $plug | Out-Null
$srcPlug = Join-Path $here "selftest_plugin"
Copy-Item (Join-Path (Join-Path $srcPlug "coop_selftest") "__init__.py") $plug -Force
Copy-Item (Join-Path $srcPlug "coop_selftest.desktop") $pykrita -Force

$log = Join-Path $here "krita_live_gui.log"
$trace = Join-Path $here "krita_live_gui.trace"
if (Test-Path $log) { Remove-Item $log }
if (Test-Path $trace) { Remove-Item $trace }
$bat = Join-Path $res "run-selftest.bat"
$kenv = Join-Path (Split-Path -Parent $here) "krita-build\windows\kenv.bat"
@"
@echo off
set KRITA_DEV=$KritaDev
call "$kenv"
set KRITA_FORCE_PYTHON_PLUGINS=coop_selftest
set COOP_SELFTEST_DIR=$here
set NOSPLASH=1
start "" "$krita" --resource-location "$res"
"@ | Set-Content -Encoding ascii $bat
& "$env:SystemRoot\System32\cmd.exe" /c $bat

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$done = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if ((Test-Path $log) -and (Select-String -Path $log -Pattern '^DONE$' -Quiet)) { $done = $true; break }
    if (-not (Get-Process krita -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $krita })) {
        Write-Host "Krita exited before the test finished."
        break
    }
}
Start-Sleep -Seconds 1
Get-Process krita -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $krita } | Stop-Process -Force
Remove-Item -Recurse -Force $plug, (Join-Path $pykrita "coop_selftest.desktop") -ErrorAction SilentlyContinue
if (Test-Path $log) { Get-Content $log } else { Write-Host "No log was written." }
if (-not $done) {
    Write-Host "TEST DID NOT FINISH"
    if (Test-Path $trace) { Get-Content $trace }
    exit 2
}
if (Select-String -Path $log -Pattern '^FAIL' -Quiet) { exit 1 }
exit 0
