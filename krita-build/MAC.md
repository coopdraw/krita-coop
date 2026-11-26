# The co-op build of Krita on macOS

**Status (2026-11-25): it builds on GitHub for Apple Silicon, and the built app passes the automatic tests on one of GitHub's Macs** (macOS 15, Apple Silicon): the engine tests, the real-API test, and the test with real brush strokes inside the app (49 checks: strokes shared while they are painted, pixels written while a stroke is held, a host + guest session with undo). It has not been used by a person on a real Mac yet, with a pen, or in a session with the Windows host. Building by hand on your own Mac has not been tried; it runs the same script.

The result has to be the same thing as on Windows: Krita 5.3.4.1 (tag `v6.0.4.1`, Qt 5) + `krita-patches/coop-core.patch` + the engine `krita_coop/` built in. The engine's protocol version must match the host's, so build from the same commit of this repository as the Windows side.

## Getting a build from GitHub

1. A build is made by hand on the repository's page: **Actions > macOS build > Run workflow**. `arm64` is for Apple Silicon and takes about an hour and a half; `universal` also runs on Intel Macs and takes about twice as long. To use a build that already exists, skip this step.
2. Open the newest green run of **macOS build** and download **krita-coop-macos-…** under **Artifacts** (you have to be signed in to GitHub; downloads are kept for 30 days). Unzip it; inside is another zip with `krita.app`.
3. The app is not signed by a registered developer, so macOS blocks it at first. After moving `krita.app` where you want it, run once in Terminal:
   ```bash
   xattr -dr com.apple.quarantine /path/to/krita.app
   ```
   Then start it normally.
4. An older "Coop Drawing" plugin may still be in Krita's resource folder on this Mac. Delete it:
   ```bash
   rm -rf ~/Library/Application\ Support/krita/pykrita/krita_coop ~/Library/Application\ Support/krita/pykrita/krita_coop.desktop
   ```

The workflow is `.github/workflows/macos.yml`. It runs the stages of `krita-build/mac/build.sh` on one of GitHub's Apple Silicon machines, keeps a compiler cache between runs, and uploads the zipped app.

A second workflow, **macOS check** (`.github/workflows/macos-check.yml`), tests an app that was already built, in about five minutes: it downloads the artifact of a build run and runs the `check` and `gui` stages on it. Run it after every build; the build itself only runs `check`.

## Building on your own Mac

Needs Xcode with its command line tools, Python 3.10 or newer, and about 15 GB. Build from Terminal.

```bash
krita-build/mac/build.sh all                 # or the stages one by one: source deps configure build package check
ARCH=arm64 krita-build/mac/build.sh all      # only for Apple Silicon: about half the build time
```

Everything goes into `~/krita-coop-build` (set `WORK` for another place). The app ends up in `~/krita-coop-build/app/krita.app`, and zipped next to it. After a change to the engine or the patch, run `source configure build package` again.

What the stages do, and where it comes from:

| Stage | What | Source of the recipe |
|---|---|---|
| `source` | clone Krita at the tag, apply the patch, copy the engine to `plugins/python/krita_coop/`, clone KDE's dependency scripts | same as on Windows |
| `deps` | Python environment; `download-macos-tools.py` (KDE's CMake 3.29, Ninja, ccache); `setup-env.py --full-krita-env` (prebuilt dependencies, Qt 5 flavour; writes the file `env`) | Krita's build instructions and `build-tools/ci-scripts/macos.yml` |
| `configure` | CMake with `macos-toolchain-krita.cmake`, deployment target 10.15, release options | the same |
| `build` | `ninja install` into `_install` | |
| `package` | Krita's `packaging/macos/macos-deploy.py` makes a self-contained `krita.app`; ad-hoc code signature; zip | KDE's CI, minus its signing and notarisation service |
| `check` | starts the packaged `kritarunner` with a clean environment: is the engine built in, does the real-API test pass | this repository |
| `gui` | starts the packaged app and runs `tests/krita_live_gui.py` in it (real brush strokes); if Krita stops making progress it prints what every thread is doing | this repository |

## What the first runs showed

* **A clean environment.** The script takes Homebrew and MacPorts off the `PATH` and tells CMake to ignore their folders (on Windows, other compilers and a newer CMake on the `PATH` broke the build).
* **Python.** KDE's CI unsets `VIRTUAL_ENV` before CMake so that Krita is built against the Python of the dependencies; the script does the same, and CMake then finds the dependencies' Python 3.13.
* **Architecture.** KDE's toolchain file builds x86_64 + arm64 on Apple Silicon; `ARCH=arm64` overrides it on the CMake command line, and that works. `universal` and `x86_64` have not been tried.
* **Dependencies.** The prebuilt ones are newer than the tag; that worked on 2026-11-25. Do not pass `-DBUILD_WITH_QT6`.
* **Signing.** The app gets an ad-hoc signature (`codesign --force --deep --sign -`); KDE signs with its certificate and notarises. An ad-hoc signed app runs, but macOS quarantines a downloaded copy until the flag is removed (step 3 above).
* **Two caches make a rebuild shorter.** Both live in `work/krita/ccache`, which the workflow keeps between runs (GitHub drops it after 7 days without a build).
  * *Compiler*: ccache, passed to CMake explicitly (KDE's generated environment alone did not switch it on). It is told to identify the compiler by its version text: by default it uses the compiler's file time, and that changes every time GitHub renews its machine image, which emptied the cache about once a week.
  * *Qt's moc*: `krita-build/mac/moccache.py`. With every compile coming out of ccache a build still took an hour, because moc needs about 3 seconds per header on these machines (the `moc` among KDE's prebuilt dependencies is an Intel program, so it runs under Rosetta, and Krita's headers pull in a lot) and CMake runs it a few thousand times. The script keeps moc's results; a result is reused only when the command and the content of every file moc had read are unchanged, and adding or removing any header anywhere starts a new cache. `COOP_NO_MOCCACHE=1 build.sh configure ...` builds without it.
  * The **macOS diagnostics** workflow (`.github/workflows/macos-diag.yml`, 15 minutes) shows where a build's time goes and, with `MOCCACHE_VERIFY`, compares every stored moc result it uses with what moc makes now.
* **kritarunner** on macOS imports its script only from Krita's own plugin folders, not from the working directory as on Windows; `check` copies the test scripts next to the engine inside the app for the run.
* **Threads.** GitHub's Macs have 3 cores, which exposed a hang in the patch that a 16-thread PC had hidden (pixels written into a layer while a finished stroke was being merged into it). It is fixed, and it is the reason to run **macOS check** after every build.
* **Runner labels.** `macos-14` no longer exists; the workflows use `macos-15`.

## Checking the result

1. Start it. **Settings > Dockers > Coop Drawing** must be there without installing anything, and during a session the docker's status must end with "Live mode: ...".
2. `build.sh check` (also run by the workflow): `tests/krita_bundled_probe.log` must say `native: True`, and `tests/krita_integration.log` must end with `RESULT: OK`.
3. Engine tests, no Krita needed: `python3 -m unittest discover -s tests` (81 tests, plus 13 for the moc cache).
4. Real brush strokes: `build.sh gui` (or the **macOS check** workflow). It puts the tiny plugin `tests/selftest_plugin/coop_selftest` next to Krita's own Python plugins, starts the app with `KRITA_FORCE_PYTHON_PLUGINS=coop_selftest`, `COOP_SELFTEST_DIR=<tests folder>` and `--resource-location <an empty folder>`, waits for `DONE` in `tests/krita_live_gui.log`, and closes Krita. Every line of that log must start with PASS.
5. A real session with the Windows host, with a pen. Nothing automatic covers this.
