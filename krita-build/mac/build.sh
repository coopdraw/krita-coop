#!/bin/bash
# Builds the co-op build of Krita on macOS, following KDE's own CI recipe for Krita
# (build-tools/ci-scripts/macos.yml in Krita's source). Used by .github/workflows/macos.yml
# and usable by hand on a Mac.
#
#   krita-build/mac/build.sh <stage> [<stage> ...]
#
# Stages, in order:
#   source     get Krita's source at the right tag, apply the patch, copy the engine in
#   deps       Python environment, KDE's build tools and prebuilt dependencies
#   configure  CMake
#   build      compile and install into $WORK/krita/_install
#   package    make a self-contained krita.app and zip it
#   check      start the packaged kritarunner and check the built-in engine
#   gui        real brush strokes inside the packaged app (tests/krita_live_gui.py); not part of "all"
#   all        source deps configure build package check
#
# Environment:
#   WORK   where everything goes (default: $HOME/krita-coop-build), about 15 GB
#   ARCH   universal (default: x86_64 + arm64 when built on Apple Silicon), arm64 or x86_64

set -euo pipefail

COOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${WORK:-$HOME/krita-coop-build}"
ARCH="${ARCH:-universal}"
KRITA_TAG="v6.0.4.1"
SRC="$WORK/krita"
APP_DIR="$WORK/app"

export MACOSX_DEPLOYMENT_TARGET="10.15"
export QMAKE_MACOSX_DEPLOYMENT_TARGET="10.15"

# Homebrew / MacPorts tools and libraries must not leak into the build
# (on Windows, other compilers and a newer CMake on the PATH broke it).
PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v -e '^/opt/homebrew' -e '^/usr/local' -e '^/opt/local' | paste -sd: -)"
export PATH

say() { printf '\n=== %s\n' "$*"; }

# The scripts KDE generates read unset variables, so they are sourced without "set -u".
enter_env() {
    set +u
    # shellcheck disable=SC1091
    source "$WORK/venv/bin/activate"
    # shellcheck disable=SC1091
    source "$SRC/_krita-tools/activate"
    if [ -f "$SRC/env" ]; then
        # shellcheck disable=SC1091
        source "$SRC/env"
    fi
    set -u
    # as in KDE's CI: CMake must find the Python of the dependencies, not this environment's
    unset VIRTUAL_ENV
}

stage_source() {
    say "Krita $KRITA_TAG + co-op patch + co-op engine"
    mkdir -p "$WORK"
    if [ ! -d "$SRC/.git" ]; then
        git clone --branch "$KRITA_TAG" --depth 1 https://invent.kde.org/graphics/krita.git "$SRC"
    fi
    cd "$SRC"
    git checkout -q -- .                      # drop an older version of the patch
    git apply "$COOP/krita-patches/coop-core.patch"
    rm -rf plugins/python/krita_coop
    mkdir -p plugins/python/krita_coop
    cp "$COOP"/krita_coop/*.py "$COOP"/krita_coop/*.html "$COOP"/krita_coop/*.desktop plugins/python/krita_coop/
    if [ ! -d krita-deps-management ]; then
        git clone --depth=1 https://invent.kde.org/packaging/krita-deps-management.git
    fi
    if [ ! -d krita-deps-management/ci-utilities ]; then
        git clone --depth=1 https://invent.kde.org/packaging/krita-ci-utilities.git krita-deps-management/ci-utilities
    fi
    git -C krita-deps-management log -1 --format='krita-deps-management at %h (%cs)'
}

stage_deps() {
    say "Python environment, build tools, prebuilt dependencies"
    cd "$SRC"
    if [ ! -d "$WORK/venv" ]; then
        python3 -m venv "$WORK/venv" --upgrade-deps
    fi
    set +u
    # shellcheck disable=SC1091
    source "$WORK/venv/bin/activate"
    set -u
    pip install -q -r krita-deps-management/requirements.txt
    python3 -u krita-deps-management/tools/download-macos-tools.py
    set +u
    # shellcheck disable=SC1091
    source _krita-tools/activate
    set -u
    cmake --version | head -1
    ninja --version
    # Qt 5 flavour: the default of this script (KDE's deps branch "master").
    # It also writes the file "env" that the later stages source.
    python3 -u krita-deps-management/tools/setup-env.py --full-krita-env -v "$WORK/venv"
    if [ -n "${CI:-}" ]; then
        rm -rf cache                          # the downloaded archives; disk space is short on CI
    fi
}

stage_configure() {
    say "Configure ($ARCH)"
    cd "$SRC"
    enter_env
    # A compiler cache makes the next build after a small change take minutes instead of hours.
    # (The variables in KDE's env file alone did not make CMake use it.)
    local cache_args=()
    if command -v ccache >/dev/null 2>&1; then
        export CCACHE_DIR="$SRC/ccache/krita"
        mkdir -p "$CCACHE_DIR"
        ccache -M "${CCACHE_MAXSIZE_COOP:-4G}" >/dev/null
        ccache -z >/dev/null
        cache_args=(-DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache)
    fi
    local arch_args=()
    case "$ARCH" in
        universal) ;;                         # the toolchain file picks x86_64 + arm64 on Apple Silicon
        arm64|x86_64) arch_args=("-DCMAKE_OSX_ARCHITECTURES=$ARCH") ;;
        *) echo "ARCH must be universal, arm64 or x86_64" >&2; exit 1 ;;
    esac
    mkdir -p _build
    cd _build
    cmake -G Ninja "$SRC" \
        -DCMAKE_BUILD_TYPE=Release \
        -DHIDE_SAFE_ASSERTS=ON -DBUILD_TESTING=OFF -DKRITA_ENABLE_PCH=OFF -DFOUNDATION_BUILD=ON \
        -DCMAKE_INSTALL_PREFIX="$SRC/_install" \
        -DCMAKE_TOOLCHAIN_FILE="$SRC/krita-deps-management/tools/macos-toolchain-krita.cmake" \
        "-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local;/opt/local" \
        ${cache_args[@]+"${cache_args[@]}"} \
        ${arch_args[@]+"${arch_args[@]}"}
}

stage_build() {
    say "Build"
    cd "$SRC"
    enter_env
    cd _build
    export CCACHE_DIR="$SRC/ccache/krita"
    ninja install
    ls -d "$SRC/_install/bin/krita.app"
}

# How much of the last build came out of the compiler cache. (Its own stage: printed at the
# end of the long build step, the numbers got lost from the log.)
stage_cachestats() {
    say "Compiler cache"
    cd "$SRC"
    enter_env
    export CCACHE_DIR="$SRC/ccache/krita"
    if command -v ccache >/dev/null 2>&1; then
        ccache -s
    else
        echo "ccache is not on the PATH"
    fi
}

# Where the build's time goes (used by the "macOS diagnostics" workflow, after "configure"):
# builds for a few minutes while printing the busiest processes, then prints ninja's timing
# log and times Qt's moc by hand (mocdiag.py).
stage_diag() {
    say "Diagnostics: where the build's time goes"
    cd "$SRC"
    enter_env
    export CCACHE_DIR="$SRC/ccache/krita"
    echo "cores: $(sysctl -n hw.ncpu)  memory: $(sysctl -n hw.memsize)  machine: $(uname -m)"
    local f
    for f in "$SRC/_install/bin/moc" "$(command -v cmake)" "$(command -v ninja)" "$(command -v ccache || true)"; do
        [ -n "$f" ] || continue
        echo "--- $f"
        file "$f" || true
        codesign -dv "$f" 2>&1 | head -n 4 || true
        xattr -l "$f" || true
    done
    cd _build
    ( while true; do
          sleep 50
          echo "--- busiest processes"
          top -l 2 -n 8 -o cpu -stats pid,command,cpu,time | tail -n 9 || true
      done ) &
    local watcher=$!
    ninja install > "$WORK/diag_ninja.out" 2>&1 &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "${DIAG_SECONDS:-480}" ]; do
        sleep 5
        waited=$((waited + 5))
    done
    kill "$pid" 2>/dev/null || true
    pkill -P "$watcher" 2>/dev/null || true
    kill "$watcher" 2>/dev/null || true
    sleep 5
    echo "--- the build after $waited s:"
    grep -E '^\[[0-9]+/[0-9]+\]' "$WORK/diag_ninja.out" | tail -n 3 || true
    python3 "$COOP/krita-build/mac/mocdiag.py" "$SRC/_build" || true
}

stage_package() {
    say "Package"
    cd "$SRC"
    enter_env
    rm -rf "$APP_DIR"
    mkdir -p "$APP_DIR"
    # Krita's own deploy script: copies frameworks, plugins and Python into the bundle
    python3 -u packaging/macos/macos-deploy.py --install-dir "$SRC/_install" --output-dir "$APP_DIR" --krita-source "$SRC"
    local app
    app="$(ls -d "$APP_DIR"/*.app | head -1)"
    # KDE signs with its own certificate. Without one, an ad-hoc signature is the minimum
    # Apple Silicon needs to run the app at all.
    codesign --force --deep --sign - "$app"
    codesign --verify --deep "$app" && echo "ad-hoc signature ok"
    local engine
    engine="$(find "$app" -type f -path '*pykrita/krita_coop/__init__.py' | head -1)"
    if [ -z "$engine" ]; then
        echo "ERROR: the co-op engine is not in the app bundle" >&2
        exit 1
    fi
    echo "co-op engine in the bundle: $engine"
    lipo -archs "$app/Contents/MacOS/krita"
    rm -f "$WORK"/krita-coop-macos-*.zip
    ditto -c -k --keepParent "$app" "$WORK/krita-coop-macos-$ARCH.zip"
    ls -lh "$WORK/krita-coop-macos-$ARCH.zip"
}

stage_check() {
    say "Check the packaged app"
    local app engine pyk t ok=0
    app="$(ls -d "$APP_DIR"/*.app | head -1)"
    engine="$(find "$app" -type f -path '*pykrita/krita_coop/__init__.py' | head -1)"
    if [ -z "$engine" ]; then
        echo "ERROR: the co-op engine is not in the app bundle" >&2
        exit 1
    fi
    pyk="$(dirname "$(dirname "$engine")")"
    rm -f "$COOP/tests/krita_bundled_probe.log" "$COOP/tests/krita_integration.log"
    # kritarunner on macOS imports scripts only from Krita's own plugin folders (not from the
    # working directory, as it does on Windows), so the test scripts are put next to the engine
    # for the run. That way they also use the engine inside the app, which is the point.
    # A clean environment: the app must work without anything from the build.
    for t in krita_bundled_probe krita_integration; do
        cp "$COOP/tests/$t.py" "$pyk/"
        rm -f "$pyk/$t.log"
        (cd "$pyk" && env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
            "$app/Contents/MacOS/kritarunner" -s "$t" -f main) > "$WORK/$t.out" 2>&1 || true
        if [ -f "$pyk/$t.log" ]; then
            cp "$pyk/$t.log" "$COOP/tests/$t.log"
            echo "--- $t.log"
            tail -n 60 "$pyk/$t.log"
        else
            echo "--- $t wrote no log; kritarunner said:"
            tail -n 40 "$WORK/$t.out"
            ok=1
        fi
        rm -f "$pyk/$t.py" "$pyk/$t.log"
    done
    grep -q '^native: True' "$COOP/tests/krita_bundled_probe.log" 2>/dev/null \
        || { echo "FAILED: the engine is not live inside the app"; ok=1; }
    grep -q '^RESULT: OK' "$COOP/tests/krita_integration.log" 2>/dev/null \
        || { echo "FAILED: krita_integration"; ok=1; }
    if [ "$ok" -eq 0 ]; then
        echo "CHECK OK"
    fi
    return "$ok"
}

stage_gui() {
    say "Real brush strokes inside the app (opens Krita for a minute or two)"
    local app engine pyk res log trace pid waited=0 limit="${GUI_TIMEOUT:-480}"
    app="$(ls -d "$APP_DIR"/*.app | head -1)"
    engine="$(find "$app" -type f -path '*pykrita/krita_coop/__init__.py' | head -1)"
    pyk="$(dirname "$(dirname "$engine")")"
    res="$WORK/testres"
    log="$COOP/tests/krita_live_gui.log"
    trace="$COOP/tests/krita_live_gui.trace"
    mkdir -p "$res"
    rm -f "$log" "$trace"
    # the plugin that starts tests/krita_live_gui.py, next to Krita's own plugins for this run
    rm -rf "$pyk/coop_selftest"
    cp -R "$COOP/tests/selftest_plugin/coop_selftest" "$pyk/coop_selftest"
    cp "$COOP/tests/selftest_plugin/coop_selftest.desktop" "$pyk/"
    KRITA_FORCE_PYTHON_PLUGINS=coop_selftest COOP_SELFTEST_DIR="$COOP/tests" NOSPLASH=1 \
        "$app/Contents/MacOS/krita" --resource-location "$res" > "$WORK/krita_gui.out" 2>&1 &
    pid=$!
    local stamp="" last="" quiet=0
    while [ "$waited" -lt "$limit" ]; do
        sleep 2
        waited=$((waited + 2))
        if [ -f "$log" ] && grep -q '^DONE$' "$log"; then
            break
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "Krita exited before the test finished"
            break
        fi
        # no progress in the log or the trace for 90 s: it hangs
        stamp="$( (stat -f '%m' "$log" "$trace" 2>/dev/null || true) | tr '\n' ' ')"
        if [ "$stamp" = "$last" ]; then
            quiet=$((quiet + 2))
        else
            quiet=0
            last="$stamp"
        fi
        if [ "$quiet" -ge 90 ]; then
            echo "no progress for $quiet s: Krita hangs. What its threads are doing:"
            sample "$pid" 3 -file "$WORK/krita_sample.txt" >/dev/null 2>&1 || true
            # the call graph, without the long list of loaded libraries at the end
            sed -n '/^Call graph:/,/^Total number in stack/p' "$WORK/krita_sample.txt" | cut -c1-260 | head -n 400 || true
            break
        fi
    done
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
    rm -rf "$pyk/coop_selftest" "$pyk/coop_selftest.desktop"
    if [ -f "$log" ]; then
        cat "$log"
    else
        echo "no log was written. Trace:"
        cat "$trace" 2>/dev/null || echo "(no trace either: the plugin did not start)"
        echo "Krita said:"
        tail -n 40 "$WORK/krita_gui.out" || true
        return 1
    fi
    if ! grep -q '^DONE$' "$log"; then
        echo "THE TEST DID NOT FINISH (after $waited s)"
        cat "$trace" 2>/dev/null || true
        return 1
    fi
    if grep -q '^FAIL' "$log"; then
        echo "GUI TEST FAILED: $(grep -c '^FAIL' "$log") check(s)"
        return 1
    fi
    echo "GUI TEST OK: $(grep -c '^PASS' "$log") checks"
}

if [ $# -eq 0 ]; then
    sed -n '2,23p' "${BASH_SOURCE[0]}"
    exit 1
fi
for stage in "$@"; do
    case "$stage" in
        source) stage_source ;;
        deps) stage_deps ;;
        configure) stage_configure ;;
        build) stage_build ;;
        package) stage_package ;;
        check) stage_check ;;
        cachestats) stage_cachestats ;;
        diag) stage_diag ;;
        gui) stage_gui ;;
        all) stage_source; stage_deps; stage_configure; stage_build; stage_package; stage_check ;;
        *) echo "unknown stage: $stage" >&2; exit 1 ;;
    esac
done
