#!/usr/bin/env bash
#
# ONTbarcoder 3 — register the extracted bundle with the Ubuntu desktop.
#
# Run it once after unpacking, from inside the bundle folder:
#     ./install.sh
#
# It does NOT copy the ~400 MB bundle anywhere: it registers the folder where
# it already lives, so ONTbarcoder appears in the applications grid and can be
# started by clicking its icon or by typing `ontbarcoder3` in a terminal.
# If you later move the folder, just run ./install.sh again from the new path.
#
#   --install-deps   also apt-install the system libraries Qt needs (asks sudo)
#   --uninstall      remove the menu entry and the command shortcut
#
set -uo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_ID="ontbarcoder3"

DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/$APP_ID.desktop"
BIN_DIR="$HOME/.local/bin"
BIN_LINK="$BIN_DIR/$APP_ID"

# Qt's xcb platform plugin links against these; the bundle does not ship them.
QT_DEPS="libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 \
libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
libxcb-shape0 libxcb-util1 libxcb-xkb1"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
err()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

do_uninstall() {
    echo "==> Removing ONTbarcoder 3 desktop integration"
    rm -f "$DESKTOP_FILE" && ok "menu entry removed"
    [ -L "$BIN_LINK" ] && rm -f "$BIN_LINK" && ok "command shortcut removed"
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null
    echo
    echo "The application folder itself was left untouched:"
    echo "  $APP_DIR"
    exit 0
}

install_deps() {
    echo "==> Installing Qt system dependencies (sudo required)"
    if ! command -v apt-get >/dev/null 2>&1; then
        err "apt-get not found — this option only works on Debian/Ubuntu."
        err "Install the equivalents of: $QT_DEPS"
        return 1
    fi
    # shellcheck disable=SC2086
    sudo apt-get update && sudo apt-get install -y $QT_DEPS
}

WANT_DEPS=0
for arg in "$@"; do
    case "$arg" in
        --uninstall)    do_uninstall ;;
        --install-deps) WANT_DEPS=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) err "Unknown option: $arg"; exit 2 ;;
    esac
done

echo "==> ONTbarcoder 3 setup"
echo "    Folder: $APP_DIR"
echo

# --------------------------------------------------------------- sanity ------
if [ ! -f "$APP_DIR/ONTbarcoder3" ]; then
    err "ONTbarcoder3 executable not found in $APP_DIR"
    err "Run this script from inside the extracted bundle folder."
    exit 1
fi

[ "$WANT_DEPS" -eq 1 ] && { install_deps || exit 1; echo; }

# ------------------------------------------------------- permissions ---------
echo "==> Fixing permissions"
chmod +x "$APP_DIR/ONTbarcoder3" 2>/dev/null && ok "ONTbarcoder3 is executable"
[ -f "$APP_DIR/launch.sh" ] && chmod +x "$APP_DIR/launch.sh" && ok "launch.sh is executable"
if [ -f "$APP_DIR/_mafftfiles/disttbfast" ]; then
    chmod +x "$APP_DIR/_mafftfiles/disttbfast" && ok "MAFFT disttbfast is executable"
else
    warn "_mafftfiles/disttbfast is missing — alignment will fail"
fi

if [ ! -x "$APP_DIR/ONTbarcoder3" ]; then
    err "Could not make the executable runnable — is this a read-only mount?"
    err "Copy the folder to your home directory and run ./install.sh again."
    exit 1
fi

# A noexec mount defeats the permission bits entirely — catch it now rather
# than letting the user click an icon that does nothing.
if command -v findmnt >/dev/null 2>&1 &&
   findmnt -no OPTIONS --target "$APP_DIR" 2>/dev/null | tr ',' '\n' | grep -qx noexec; then
    err "This folder sits on a filesystem mounted 'noexec'; programs cannot run from it."
    err "Move it to your home directory first:"
    err "  cp -r \"$APP_DIR\" ~/ && cd ~/$(basename "$APP_DIR") && ./install.sh"
    exit 1
fi

# ------------------------------------------------------- glibc --------------
# The single most common reason a bundle refuses to start: it was built on a
# newer distribution than the one it is being installed on.
echo
echo "==> Checking glibc compatibility"
ver_le() { [ "$1" = "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" ]; }
GLIBC_SYS="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
PYLIB="$(ls "$APP_DIR"/_internal/libpython3*.so* 2>/dev/null | head -1)"
if [ -n "$GLIBC_SYS" ] && [ -n "$PYLIB" ]; then
    GLIBC_REQ="$(grep -aoE 'GLIBC_2\.[0-9]+' "$PYLIB" 2>/dev/null |
                 sed 's/GLIBC_//' | sort -V | tail -1)"
    if [ -n "$GLIBC_REQ" ] && ! ver_le "$GLIBC_REQ" "$GLIBC_SYS"; then
        err "This bundle needs glibc $GLIBC_REQ but this system has $GLIBC_SYS."
        err "glibc is not forward compatible — the bundle cannot run here."
        err "Rebuild it on this machine (or on any system with glibc <= $GLIBC_SYS):"
        err "  ./build_linux.sh"
        exit 1
    fi
    ok "glibc $GLIBC_SYS satisfies the required $GLIBC_REQ"
else
    warn "could not determine glibc versions"
fi

# ------------------------------------------------------- dependencies --------
echo
echo "==> Checking Qt system libraries"
QXCB="$APP_DIR/_internal/PyQt5/Qt5/plugins/platforms/libqxcb.so"
if [ -f "$QXCB" ] && command -v ldd >/dev/null 2>&1; then
    missing="$(LD_LIBRARY_PATH="$APP_DIR/_internal:${LD_LIBRARY_PATH:-}" \
               ldd "$QXCB" 2>/dev/null | awk '/not found/ {print $1}' | sort -u)"
    if [ -n "$missing" ]; then
        warn "Missing shared libraries:"
        printf '      %s\n' $missing
        echo
        warn "Install them with:"
        # shellcheck disable=SC2086
        echo "      sudo apt install $(echo $QT_DEPS)"
        echo
        warn "…or re-run:  ./install.sh --install-deps"
        DEPS_MISSING=1
    else
        ok "all Qt dependencies satisfied"
        DEPS_MISSING=0
    fi
else
    warn "could not verify (ldd or the xcb plugin is unavailable)"
    DEPS_MISSING=0
fi

# ------------------------------------------------------- desktop entry -------
echo
echo "==> Registering the application"
mkdir -p "$DESKTOP_DIR" "$BIN_DIR"

if [ -f "$APP_DIR/ONTbarcoder3.desktop.in" ]; then
    sed "s|@APPDIR@|$APP_DIR|g" "$APP_DIR/ONTbarcoder3.desktop.in" >"$DESKTOP_FILE"
else
    cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=ONTbarcoder 3
GenericName=DNA Barcode Assembler
Comment=Assemble DNA barcodes from Oxford Nanopore reads
Exec=$APP_DIR/launch.sh
Icon=$APP_DIR/icon.svg
Path=$APP_DIR
Terminal=false
StartupNotify=true
StartupWMClass=ONTbarcoder3
Categories=Science;Biology;Education;
Keywords=DNA;barcode;nanopore;ONT;MAFFT;sequencing;
EOF
fi
chmod +x "$DESKTOP_FILE"
ok "menu entry: $DESKTOP_FILE"

ln -sf "$APP_DIR/launch.sh" "$BIN_LINK"
ok "terminal command: $BIN_LINK"

command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null

# GNOME refuses to launch a .desktop file it does not trust when it is double
# clicked from a file manager; marking it trusted removes that prompt.
command -v gio >/dev/null 2>&1 && gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in your PATH — log out and back in, or run:"
       echo "        export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

echo
if [ "${DEPS_MISSING:-0}" -eq 1 ]; then
    warn "Setup finished, but install the missing libraries above before launching."
else
    ok "Setup finished."
fi
echo
echo "  Launch it from the Activities / applications grid  →  \"ONTbarcoder 3\""
echo "  or from a terminal                                 →  $APP_ID"
echo "  or directly                                        →  $APP_DIR/launch.sh"
echo
echo "  Startup log:  ${XDG_STATE_HOME:-$HOME/.local/state}/ONTbarcoder/launcher.log"
echo "  To undo:      ./install.sh --uninstall"
