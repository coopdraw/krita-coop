@echo off
rem Build + install the co-op build into %KRITA_DEV%\_install (incremental after the first run).
rem Copies the plugin from this repository into the source tree first.
rem Writes ninja's exit code to %KRITA_DEV%\build.status when it is done.
call "%~dp0kenv.bat"
if exist "%KD%\build.status" del /q "%KD%\build.status"
call "%~dp0sync-plugin.bat"
if errorlevel 1 (
  (echo 1)> "%KD%\build.status"
  exit /b 1
)
cd /d "%KD%\b_krita"
ninja -j12 -k 0 install
(echo %ERRORLEVEL%)> "%KD%\build.status"
