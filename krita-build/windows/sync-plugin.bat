@echo off
rem Copies the co-op plugin from this repository into Krita's source tree
rem (plugins\python\krita_coop), from where the build installs it like Krita's own Python plugins.
call "%~dp0kenv.bat"
set "DEST=%KD%\krita\plugins\python\krita_coop"
if exist "%DEST%" rmdir /s /q "%DEST%"
mkdir "%DEST%" || exit /b 1
copy /y "%COOP_REPO%\krita_coop\*.py" "%DEST%\" >nul || exit /b 1
copy /y "%COOP_REPO%\krita_coop\*.html" "%DEST%\" >nul || exit /b 1
copy /y "%COOP_REPO%\krita_coop\*.desktop" "%DEST%\" >nul || exit /b 1
