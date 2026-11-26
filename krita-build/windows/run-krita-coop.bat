@echo off
rem Starts the freshly built Krita straight from the build tree (not the packaged copy).
call "%~dp0kenv.bat"
start "" "%KD%\_install\bin\krita.exe" %*
