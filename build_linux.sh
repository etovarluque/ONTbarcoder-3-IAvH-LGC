#!/usr/bin/env bash
#
# Build a distributable Linux bundle of ONTbarcoder3 with PyInstaller.
#
# Run this ON the Ubuntu/Linux machine (PyInstaller does NOT cross-compile —
# the disttbfast binary and the produced executable are Linux ELF files).
#
#   chmod +x build_linux.sh
#   ./build_linux.sh
#
# Result:  dist/ONTbarcoder3/       -> self-contained folder, run ./launch.sh
#          dist/ONTbarcoder3.tar.gz -> the same folder, ready to ship
#
# ---------------------------------------------------------------------------
# IMPORTANT — glibc portability
#
# The bundle can only run on a system whose glibc is at least as new as the one
# on THIS build machine; glibc is backward compatible but never forward
# compatible.  Building on Ubuntu 24.04 (glibc 2.39) produces a bundle that
# fails on Ubuntu 22.04 (glibc 2.35) with:
#
#     [PYI-2303:ERROR] Failed to load Python shared library ...
#     version `GLIBC_2.38' not found
#
# So: build on the OLDEST distribution you intend to support.  The final step
# of this script prints exactly which glibc the result requires.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

APP="ONTbarcoder3"
VENV=".build-venv"

echo "==> [1/7] Python virtual environment"
# Ubuntu splits venv out of the base python package; without it the error you
# get is a confusing "ensurepip is not available".
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    echo "ERROR: python3-venv is not installed. Run:" >&2
    echo "  sudo apt install python3-venv python3-dev build-essential" >&2
    exit 1
fi

# Recreate the venv unless it is a working one for THIS machine.  A .build-venv
# copied over from another OS (e.g. a Windows checkout, where it holds
# Scripts/ instead of bin/) would otherwise break `source` below.
if [ ! -x "$VENV/bin/python" ]; then
    [ -e "$VENV" ] && { echo "    (removing unusable $VENV)"; rm -rf "$VENV"; }
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt pyinstaller

echo "==> [2/7] Clean previous build"
rm -rf build "dist/$APP" "dist/$APP.tar.gz"

echo "==> [3/7] Run PyInstaller"
pyinstaller --noconfirm "${APP}_linux.spec"

DIST="dist/$APP"

echo "==> [4/7] Copy runtime data next to the executable"
# These folders are located at runtime via dirname(sys.executable), so they
# must sit alongside the executable — NOT inside _internal/.
cp -r _mafftfiles          "$DIST/"
# The Windows MAFFT binary is in the same folder (both builds share these
# sources); it is dead weight in a Linux bundle.
rm -f "$DIST/_mafftfiles/disttbfast.exe"
[ -d guide ]         && cp -r guide       "$DIST/"
[ -d translations ]  && cp -r translations "$DIST/"
# _profiles / _notes / _ui_cache are (re)created at runtime, but ship the ones
# that already hold content so a fresh install starts with the reference notes
# (Cytb.md, ITS.md, rbcL.md, ...) and the saved parameter profiles.
[ -d _profiles ] && cp -r _profiles "$DIST/"
[ -d _notes ]    && cp -r _notes    "$DIST/"

echo "==> [5/7] Install the Linux launcher and desktop integration"
# launch.sh is what the .desktop entry runs: it repairs lost permission bits,
# picks the Qt platform plugin, checks glibc and the xcb system libraries, and
# turns a silent startup crash into a visible dialog + a log file.
cp linux/launch.sh              "$DIST/"
cp linux/install.sh             "$DIST/"
cp linux/icon.svg               "$DIST/"
cp linux/ONTbarcoder3.desktop.in "$DIST/"
[ -f linux/README-LINUX.md ] && cp linux/README-LINUX.md "$DIST/README.md"

# Guard against CRLF sneaking in from a Windows checkout — a shell script with
# CRLF line endings dies on Linux with "bad interpreter: No such file".
for s in "$DIST/launch.sh" "$DIST/install.sh"; do
    sed -i 's/\r$//' "$s"
done

echo "==> [6/7] Fix executable permissions"
chmod +x "$DIST/_mafftfiles/disttbfast"
chmod +x "$DIST/$APP"
chmod +x "$DIST/launch.sh" "$DIST/install.sh"

echo "==> [7/7] Package"
tar czf "dist/$APP.tar.gz" -C dist "$APP"

deactivate || true

# --------------------------------------------------------------- report -----
GLIBC_BUILD="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
PYLIB="$(ls "$DIST"/_internal/libpython3*.so* 2>/dev/null | head -1)"
GLIBC_REQ=""
[ -n "$PYLIB" ] && GLIBC_REQ="$(grep -aoE 'GLIBC_2\.[0-9]+' "$PYLIB" 2>/dev/null |
                                sed 's/GLIBC_//' | sort -V | tail -1)"

echo
echo "Done.  Distributable bundle:  $DIST/"
echo "       Shippable archive:     dist/$APP.tar.gz"
echo
echo "Built on glibc ${GLIBC_BUILD:-unknown}; the bundle requires glibc >= ${GLIBC_REQ:-unknown}."
echo "It will NOT run on any machine with an older glibc than that."
echo
echo "Test it here with:            ./$DIST/launch.sh"
echo
echo "On the target machine:"
echo "  tar xzf $APP.tar.gz"
echo "  cd $APP"
echo "  ./install.sh          # adds it to the applications menu"
