@echo off
rem Makes the self-contained copy of the co-op build: %KRITA_DEV%\pkg\krita-coop-x64-5.3.4.1\ and the
rem same as a .zip (for another Windows PC), with Krita's own packaging script. About 5 minutes.
rem Krita must not be running from the old package. Writes the exit code to %KRITA_DEV%\package.status.
call "%~dp0kenv.bat"
if exist "%KD%\package.status" del /q "%KD%\package.status"
if not exist "%KD%\pkg" mkdir "%KD%\pkg"
cd /d "%KD%\pkg"
if exist krita-coop-x64-5.3.4.1 rmdir /s /q krita-coop-x64-5.3.4.1
if exist krita-coop-x64-5.3.4.1.zip del /q krita-coop-x64-5.3.4.1.zip
if exist krita-coop-x64-5.3.4.1-dbg.zip del /q krita-coop-x64-5.3.4.1-dbg.zip
python "%KD%\krita\packaging\windows\package-complete.py" --no-interactive --package-name krita-coop-x64-5.3.4.1 --src-dir "%KD%\krita" --deps-install-dir "%KD%\_install" --krita-install-dir "%KD%\_install"
(echo %ERRORLEVEL%)> "%KD%\package.status"
