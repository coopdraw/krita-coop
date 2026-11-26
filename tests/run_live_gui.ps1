# Runs tests/krita_live_gui.py inside the co-op build of Krita (real application, real brush strokes).
#
#   powershell -ExecutionPolicy Bypass -File tests\run_live_gui.ps1
#
# Krita is started with its own resource folder (default brushes). A small plugin that runs the test is
# put into the build's plugin folder for the duration of the test and enabled through
# KRITA_FORCE_PYTHON_PLUGINS, so your Krita settings are not changed.
# A Krita window opens for the duration of the test (about a minute) and is closed again.
param(
    [string]$KritaDev = "D:\krita-dev",
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
@"
[Desktop Entry]
Type=Service
ServiceTypes=Krita/PythonPlugin
X-KDE-Library=coop_selftest
X-Python-2-Compatible=false
Name=Coop self-test
Comment=Runs tests/krita_live_gui.py (only when started by tests/run_live_gui.ps1).
"@ | Set-Content -Encoding ascii (Join-Path $pykrita "coop_selftest.desktop")
@"
import os
import sys
import time
import traceback

from krita import Extension, Krita
from PyQt5.QtCore import QTimer

DIR = os.environ.get("COOP_SELFTEST_DIR")


def trace(text):
    if DIR:
        with open(os.path.join(DIR, "krita_live_gui.trace"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))


class SelfTest(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.started = False
        self.tries = 0

    def setup(self):
        trace("setup")

    def createActions(self, window):
        trace("createActions")
        if not self.started and DIR:
            self.started = True
            QTimer.singleShot(4000, self.run)

    def run(self):
        # (an exception escaping a Qt slot would open Krita's modal error dialog)
        try:
            if Krita.instance().activeWindow() is None and self.tries < 120:
                self.tries += 1
                if self.tries % 10 == 1:
                    trace("no active window yet")
                QTimer.singleShot(1000, self.run)
                return
            trace("running")
            sys.path.insert(0, DIR)
            import krita_live_gui
            krita_live_gui.main()
            trace("finished")
        except BaseException:  # noqa: BLE001
            trace("EXCEPTION\n" + traceback.format_exc())


Krita.instance().addExtension(SelfTest(Krita.instance()))
"@ | Set-Content -Encoding ascii (Join-Path $plug "__init__.py")

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
