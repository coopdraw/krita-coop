@echo off
rem One-time: create the Python environment and download Krita's prebuilt dependencies
rem (Qt 5 flavour, KDE's deps branch "master") into %KRITA_DEV%\_install. About 1.5 GB.
if not defined KRITA_DEV set "KRITA_DEV=D:\krita-dev"
set "KD=%KRITA_DEV%"
cd /d "%KD%" || exit /b 1
rem no other compilers, CMakes or Pythons on the PATH while the environment is generated
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0;C:\Program Files\Git\cmd;%KD%\tools\cmake\bin"
set PYTHONHOME=
set PYTHONPATH=
if not exist PythonEnv\Scripts\python.exe (
  tools\Python-3.13\python.exe -m venv PythonEnv --upgrade-deps || exit /b 1
)
call "%KD%\PythonEnv\Scripts\activate.bat"
python -m pip install -q -r krita-deps-management\requirements.txt || exit /b 1
python -m pip install -q -r krita-deps-management\ci-utilities\requirements-windows.txt || exit /b 1
python krita-deps-management\tools\setup-env.py --full-krita-env -v PythonEnv ^
  -p "%KD%\tools\llvm-mingw\bin" ^
  -p "%KD%\tools\llvm-mingw\x86_64-w64-mingw32\bin" ^
  -p "%KD%\tools\ninja" ^
  -p "%KD%\tools\cmake\bin" || exit /b 1
echo SETUP-DEPS-OK
