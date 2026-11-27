# Building the co-op build of Krita

The co-op build is Krita **5.3.4.1** (git tag `v6.0.4.1`, built with Qt 5) plus two things from this repository:

1. `krita-patches/coop-core.patch`, applied to Krita's source;
2. the co-op engine `krita_coop/`, copied into Krita's source as `plugins/python/krita_coop/`, from where the build installs it like Krita's own Python plugins. The patch adds it to `plugins/python/CMakeLists.txt` and switches it on by default.

This page is for Windows. For macOS see [MAC.md](MAC.md).

## Windows

Everything lives in one build tree: by default the folder `krita-dev` in this repository, which git ignores (set `KRITA_DEV` to use another place; no spaces, and not much deeper than this, because the longest path inside the tree is about 170 characters and Windows tools stop at 260). Nothing is installed system-wide. The tree cannot simply be moved: the build folder and the Python environment store their location, see "Moving the build tree" below. The first build takes about 35 minutes on an 8-core laptop and the tree ends up at about 16 GB with the package.

| In the build tree | What |
|---|---|
| `krita\` | Krita's source, branch `coop` = tag `v6.0.4.1` + the patch (as uncommitted changes) + the copied engine |
| `_install\` | KDE's prebuilt dependencies, and the built Krita (`_install\bin\krita.exe`) |
| `b_krita\` | build folder |
| `pkg\` | the self-contained copy that is actually used (`package.bat`) |
| `tools\` | llvm-mingw 20251118 (clang 21), CMake 3.31.8, Ninja 1.12.1, Python 3.13.17 |
| `PythonEnv\`, `krita-deps-management\` | KDE's scripts that download the dependencies, and their Python environment |
| `testres\` | resource folder used only by `tests\run_live_gui.ps1` |

### Day to day

The scripts are in `krita-build\windows\` and can be started from anywhere. They set their own clean `PATH`: other compilers, CMake 4 or another Python on the `PATH` break the build.

```bat
krita-build\windows\build.bat             :: copy the engine into the source tree, then build + install (incremental)
krita-build\windows\package.bat           :: make the self-contained copy in pkg\ (about 5 minutes)
krita-build\windows\run-krita-coop.bat    :: start the freshly built Krita straight from the build tree
```

`build.bat` writes ninja's exit code to `%KRITA_DEV%\build.status`, `package.bat` to `package.status`.

After **any** change, to the engine or to the C++ source, run `build.bat` and then `package.bat`: the engine is part of the build, and the package is a copy. `package.bat` runs Krita's own packaging script and produces `pkg\krita-coop-x64-5.3.4.1\` (start `bin\krita.exe`), the same as `krita-coop-x64-5.3.4.1.zip` for another Windows PC, and a `-dbg.zip` with debug symbols. The package needs nothing from the build tree.

After changing the C++ source, export the patch again (the engine is not part of the patch):

```bat
cd /d <build tree>\krita
git diff > <this repository>\krita-patches\coop-core.patch
```

Windows Firewall rules name the program's full path. Hosting needs an inbound TCP allow rule for `pkg\krita-coop-x64-5.3.4.1\bin\krita.exe` on every network profile partners connect through. If Windows asks, tick both private and public networks; a "block" rule made by answering that prompt with private only overrides allow rules for the public profile.

### Moving the build tree

Done once, on 2026-11-27 (from another drive into this repository). The package in `pkg\` can be copied anywhere; the tree needs these steps:

1. Close Krita. Copy the tree without `b_krita\` (the build folder stores its location and has to be made again) and without the `env*.bat` and `base-env*.bat` files KDE's setup generated (they hold the old paths and are not used; `kenv.bat` replaces them).
2. Delete `PythonEnv\` in the copy and run `krita-build\windows\setup-deps.bat python`: it makes the Python environment again without touching the dependencies. The dependencies in `_install\`, the tools and Krita's source do not store their location.
3. `configure.bat`, then `build.bat` (a full build, about 35 minutes), the tests, then `package.bat`.
4. Windows Firewall rules name the program's full path (see above): point them at the new `pkg\...\bin\krita.exe`.

### From scratch

Needs Git for Windows and 7-Zip.

1. Tools, unpacked into `krita-dev\tools\` as `llvm-mingw`, `cmake`, `ninja`, `Python-3.13`:
   * <https://github.com/mstorsjo/llvm-mingw/releases/download/20251118/llvm-mingw-20251118-ucrt-x86_64.zip>
   * <https://github.com/Kitware/CMake/releases/download/v3.31.8/cmake-3.31.8-windows-x86_64.zip> (CMake 4 does not work)
   * <https://github.com/ninja-build/ninja/releases/download/v1.12.1/ninja-win.zip>
   * Python 3.13: the `tools` folder of <https://api.nuget.org/v3-flatcontainer/python/3.13.17/python.3.13.17.nupkg> (a zip). It must be 3.13.
2. Sources:
   ```bat
   cd /d <this repository>\krita-dev
   git clone https://invent.kde.org/packaging/krita-deps-management.git
   git clone https://invent.kde.org/packaging/krita-ci-utilities.git krita-deps-management/ci-utilities
   git clone --branch v6.0.4.1 --depth 1 https://invent.kde.org/graphics/krita.git krita
   cd krita
   git checkout -b coop
   git apply <this repository>\krita-patches\coop-core.patch
   echo plugins/python/krita_coop/>> .git\info\exclude
   ```
3. `krita-build\windows\setup-deps.bat` creates `PythonEnv` and downloads the dependencies (about 1.5 GB, Qt 5 flavour, KDE's deps branch `master`).
4. `krita-build\windows\configure.bat`, then `build.bat`, then `package.bat`.

The options are those of KDE's Windows CI (`-DKRITA_ENABLE_PCH=OFF -DFOUNDATION_BUILD=ON`), with `RelWithDebInfo`, no unit tests, and `-DHIDE_SAFE_ASSERTS=ON` as in releases. Krita's own instructions: <https://docs.krita.org/en/untranslatable_pages/building_krita.html>.

### Tests

```powershell
python -m unittest discover -s tests                                # engine, on fake documents
krita-build\windows\kritarunner-test.bat krita_integration          # real Krita API, no window
powershell -ExecutionPolicy Bypass -File tests\run_live_gui.ps1     # real brush strokes; opens Krita for a minute
```

`run_live_gui.ps1` is the one that proves the build: it runs inside the real application with the engine that was built in, paints with Krita's own brush engine in Wash and Build-up mode and with a mouse-driven stroke that stays open like a pen stroke, and checks that strokes are seen while they are painted, that pixels written while a stroke is held are not lost, and a whole host + guest session including undo (49 checks). The same test runs on macOS through `krita-build/mac/build.sh gui`; run it on both after changing the patch, because the two machines hit different timing. It uses its own resource folder and does not change your Krita settings.

## What the patch adds

| Where | What |
|---|---|
| `KisImage::pauseProcessingForCoop(timeoutMs)` | holds strokes and canvas updates between two jobs without ending the stroke; gives up and returns false if the running jobs don't finish in time; `unlock()` resumes |
| `KisImage::setCoopDirtyTracking()`, `takeCoopDirtyRects()` | per-layer record of every `setDirty()` rect |
| `KisImage::hasStrokesForCoop()`, `currentStrokeId()` | "is a stroke running, and which kind" without locking |
| `KisIndirectPaintingSupport::tryLockTemporaryTargetForRead()` | non-blocking read lock on the Wash overlay |
| `Node.coopPixelData()`, `coopOverlayActive()`, `coopSetPixelData()` | Python: read with the Wash stroke in progress; write with an async update of that area |
| `Document.coopApiVersion()`, `coopSetTracking()`, `coopTakeDirty()`, `coopIsIdle()`, `coopHasStrokes()`, `coopActivity()`, `coopPause()`, `coopResume()` | Python side of the above |
| `plugins/python/CMakeLists.txt`, `PythonPluginManager` | installs `krita_coop` with Krita's own plugins, on by default; a module described twice (an old copy in the user's folder) is listed once |
| `KRITA_FORCE_PYTHON_PLUGINS` | environment variable that enables Python plugins for one run (used by the tests) |

Two things to know when changing this:

* `pauseProcessingForCoop()` must not call `KisBusyWaitBroker::notifyWaitOnImageStarted()`. That one waits until the whole stroke has ended.
* While a finished Wash stroke is merged into its layer, Krita relies on nobody requesting a canvas update for that layer: the merge holds the indirect-painting lock over several jobs, an update of the layer waits for that lock in a worker thread, and once all worker threads wait, the merge can never continue. That is why `Node.coopSetPixelData()` refuses to write during a merge (returns false), and why the pause has a time limit. Both `coopPause()` and `coopSetPixelData()` returning false mean "try again shortly".
