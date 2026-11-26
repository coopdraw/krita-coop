@echo off
rem Clean build/run environment for the co-op build of Krita. Called by the other scripts.
rem The build tree is %KRITA_DEV% (default D:\krita-dev); this repository is %COOP_REPO%.
rem It replaces the env.bat that KDE's setup script generates: that one freezes the whole PATH of the
rem machine, and other compilers, CMake 4 or another Python on the PATH break the build.
if not defined KRITA_DEV set "KRITA_DEV=D:\krita-dev"
set "KD=%KRITA_DEV%"
for %%I in ("%~dp0..\..") do set "COOP_REPO=%%~fI"
set "PATH=%KD%\_install\bin;%KD%\_install\lib;%KD%\PythonEnv\Scripts;%KD%\tools\llvm-mingw\bin;%KD%\tools\llvm-mingw\x86_64-w64-mingw32\bin;%KD%\tools\ninja;%KD%\tools\cmake\bin;%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0;C:\Program Files\Git\cmd;C:\Program Files\7-Zip"
set "VIRTUAL_ENV=%KD%\PythonEnv"
set "CMAKE_PREFIX_PATH=%KD%\_install"
set "PKG_CONFIG_PATH=%KD%\_install\lib\pkgconfig;%KD%\_install\share\pkgconfig"
set "XDG_DATA_DIRS=%KD%\_install\share"
set "QMAKEFEATURES=%KD%\_install\mkspecs/features"
set "PYTHONPATH=%KD%\_install\lib\site-packages"
set "QT_DATA_DIRS=%KD%\_install\share"
set XDG_CURRENT_DESKTOP=KDE
set PYTHONDONTWRITEBYTECODE=1
set PYTHONUTF8=1
set PYTHONHOME=
set CONDA_EXE=
set CONDA_BAT=
