@echo off
rem Usage: kritarunner-test.bat <module>     e.g.  kritarunner-test.bat krita_integration
rem Runs tests\<module>.py (its main()) in the co-op build's kritarunner; the result is tests\<module>.log.
call "%~dp0kenv.bat"
cd /d "%COOP_REPO%\tests"
"%KD%\_install\bin\kritarunner.exe" -s %1 -f main
