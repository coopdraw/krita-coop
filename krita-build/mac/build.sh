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
#   all        all of the above
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
    if command -v ccache >/dev/null 2>&1 && [ -n "${CCACHE_DIR:-}" ]; then
        ccache -M "${CCACHE_MAXSIZE_COOP:-4G}" >/dev/null
        ccache -z >/dev/null
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
        ${arch_args[@]+"${arch_args[@]}"}
}

stage_build() {
    say "Build"
    cd "$SRC"
    enter_env
    cd _build
    ninja install
    if command -v ccache >/dev/null 2>&1 && [ -n "${CCACHE_DIR:-}" ]; then
        ccache -s || true
    fi
    ls -d "$SRC/_install/bin/krita.app"
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
    local app
    app="$(ls -d "$APP_DIR"/*.app | head -1)"
    cd "$COOP/tests"
    rm -f krita_bundled_probe.log krita_integration.log
    # a clean environment: the app must work without anything from the build
    env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" "$app/Contents/MacOS/kritarunner" -s krita_bundled_probe -f main || true
    cat krita_bundled_probe.log 2>/dev/null || echo "no probe log"
    env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" "$app/Contents/MacOS/kritarunner" -s krita_integration -f main || true
    tail -5 krita_integration.log 2>/dev/null || echo "no integration log"
    grep -q '^native: True' krita_bundled_probe.log
    grep -q '^RESULT: OK' krita_integration.log
}

if [ $# -eq 0 ]; then
    sed -n '2,22p' "${BASH_SOURCE[0]}"
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
        all) stage_source; stage_deps; stage_configure; stage_build; stage_package; stage_check ;;
        *) echo "unknown stage: $stage" >&2; exit 1 ;;
    esac
done
