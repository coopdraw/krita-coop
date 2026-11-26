@echo off
rem Configure the co-op build (Qt 5 flavour = Krita 5.3.x), with the options of KDE's Windows CI.
call "%~dp0kenv.bat"
call "%~dp0sync-plugin.bat" || exit /b 1
if not exist "%KD%\b_krita" mkdir "%KD%\b_krita"
cd /d "%KD%\b_krita"
cmake "%KD%\krita" -G Ninja "-DCMAKE_INSTALL_PREFIX=%KD:\=/%/_install" -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_TESTING=OFF -DKRITA_ENABLE_PCH=OFF -DFOUNDATION_BUILD=ON -DHIDE_SAFE_ASSERTS=ON || exit /b 1
echo CONFIGURE-OK
