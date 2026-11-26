# The co-op build of Krita on macOS

**Status: not built yet.** The build script and the GitHub workflow below follow KDE's own CI recipe for Krita on macOS, but have not completed a run. Update this page with what actually worked.

The result has to be the same thing as on Windows: Krita 5.3.4.1 (tag `v6.0.4.1`, Qt 5) + `krita-patches/coop-core.patch` + the engine `krita_coop/` built in. The engine's protocol version must match the host's, so build from the same commit of this repository as the Windows side.

## Getting a build from GitHub

1. On the repository's page: **Actions > macOS build > Run workflow**. Choose `universal` (runs on every Mac) or `arm64` / `x86_64` (one kind of Mac, about half the build time).
2. When the run has finished, open it and download **krita-coop-macos-…** under **Artifacts** (you have to be signed in to GitHub). Unzip it; inside is another zip with `krita.app`.
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

## Things that needed care on Windows, or are guesses here

* **A clean environment.** On Windows other compilers and a newer CMake on the `PATH` broke the build. The script takes Homebrew and MacPorts off the `PATH` and tells CMake to ignore their folders.
* **Python.** KDE's CI unsets `VIRTUAL_ENV` before CMake so that Krita is built against the Python of the dependencies; the script does the same.
* **Universal builds** are what KDE's toolchain file does on Apple Silicon; `ARCH=arm64` overrides it on the CMake command line. Whether that override is enough has not been tried.
* **Dependencies.** The prebuilt ones are newer than the tag; on Windows that worked on 2026-11-24. Do not pass `-DBUILD_WITH_QT6`.
* **Signing.** `codesign --force --deep --sign -` is a guess at the minimum; KDE signs every binary with its certificate.
* **The patch** touches threading in Krita's stroke scheduler and has only been exercised on Windows.

## Checking the result

1. Start it. **Settings > Dockers > Coop Drawing** must be there without installing anything, and during a session the docker's status must end with "Live mode: ...".
2. `build.sh check` (also run by the workflow): `tests/krita_bundled_probe.log` must say `native: True`, and `tests/krita_integration.log` must end with `RESULT: OK`.
3. Engine tests, no Krita needed: `python3 -m unittest discover -s tests` (79 tests).
4. Real brush strokes: `tests/krita_live_gui.py` has to run inside the application. On Windows `tests/run_live_gui.ps1` does that: it puts a tiny plugin `coop_selftest` next to Krita's own Python plugins, starts Krita with `KRITA_FORCE_PYTHON_PLUGINS=coop_selftest`, `COOP_SELFTEST_DIR=<tests folder>` and `--resource-location <an empty folder>`, waits for `DONE` in `tests/krita_live_gui.log`, and closes Krita. A shell version still has to be written for macOS.
5. A real session with the Windows host, with a pen.
