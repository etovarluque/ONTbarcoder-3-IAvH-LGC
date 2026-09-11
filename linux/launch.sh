#!/usr/bin/env bash
#
# ONTbarcoder 3 — launcher for Linux.
#
# This script sits NEXT TO the ONTbarcoder3 executable inside the distributed
# bundle and is what the .desktop entry actually runs.  Its job is to turn the
# silent-failure modes into a visible message:
#
#   1. the bundle was built against a newer glibc than this machine has,
#   2. the executable bit was lost in transit (copied from Windows/NTFS/zip),
#   3. the Qt "xcb" platform plugin cannot load because system libs are absent,
#   4. the app crashed at startup — invisible because the binary is built with
#      console=False, so stderr goes nowhere when launched from the desktop.
#
# Everything the app prints is appended to ~/.local/state/ONTbarcoder/launcher.log
#
set -uo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BIN="$APP_DIR/ONTbarcoder3"
QXCB="$APP_DIR/_internal/PyQt5/Qt5/plugins/platforms/libqxcb.so"

LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ONTbarcoder"
LOG="$LOG_DIR/launcher.log"
mkdir -p "$LOG_DIR" 2>/dev/null || { LOG_DIR="${TMPDIR:-/tmp}"; LOG="$LOG_DIR/ONTbarcoder-launcher.log"; }

# Keep the log from growing without bound across launches.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    mv -f "$LOG" "$LOG.1" 2>/dev/null || : >"$LOG"
fi

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }

# Show a message the user can actually see, whether launched from a menu icon
# or from a terminal.  Falls back through the dialog tools Ubuntu may have.
show_error() {
    local msg="$1"
    printf '%s\n' "$msg" >&2
    log "ERROR: $msg"
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        if command -v zenity >/dev/null 2>&1; then
            zenity --error --no-markup --width=560 \
                   --title="ONTbarcoder 3" --text="$msg" >/dev/null 2>&1
        elif command -v kdialog >/dev/null 2>&1; then
            kdialog --error "$msg" >/dev/null 2>&1
        elif command -v xmessage >/dev/null 2>&1; then
            printf '%s\n' "$msg" | xmessage -center -file - >/dev/null 2>&1
        fi
    fi
}

die() { show_error "$1"$'\n\n'"Log: $LOG"; exit 1; }

ver_le() { [ "$1" = "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" ]; }

log "=== launch: APP_DIR=$APP_DIR ==="

# --------------------------------------------------------------- 1. binary --
[ -f "$BIN" ] || die "ONTbarcoder3 executable not found:
  $BIN

The folder looks incomplete. Extract the package again with:
  tar xzf ONTbarcoder3_linux.tar.gz"

# Restore the executable bit if a Windows copy, a zip tool or an NTFS/exFAT
# volume dropped it.  A very common cause of \"double click does nothing\".
for f in "$BIN" "$APP_DIR/_mafftfiles/disttbfast"; do
    if [ -e "$f" ] && [ ! -x "$f" ]; then
        log "chmod +x $f"
        chmod +x "$f" 2>/dev/null || true
    fi
done

if [ ! -x "$BIN" ]; then
    die "The executable is not runnable and the permission could not be set:
  $BIN

Run this in a terminal:
  chmod +x \"$BIN\""
fi

# A noexec mount (many NTFS/exFAT/USB/network mounts) silently refuses to run
# the binary no matter what the permission bits say.
if command -v findmnt >/dev/null 2>&1; then
    if findmnt -no OPTIONS --target "$APP_DIR" 2>/dev/null | tr ',' '\n' | grep -qx noexec; then
        die "This folder is on a filesystem mounted with 'noexec',
which forbids running programs from it:

  $APP_DIR

Copy the folder to your home directory and start it from there:
  cp -r \"$APP_DIR\" ~/ONTbarcoder3 && ~/ONTbarcoder3/launch.sh"
    fi
fi

# --------------------------------------------------------------- 2. glibc ---
# glibc is backward compatible but never forward compatible: a bundle built on
# a newer distribution aborts with a cryptic PYI-2303 error before any window
# exists.  Detect it up front and say what to do about it.
GLIBC_SYS="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
PYLIB="$(ls "$APP_DIR"/_internal/libpython3*.so* 2>/dev/null | head -1)"
if [ -n "$GLIBC_SYS" ] && [ -n "$PYLIB" ]; then
    GLIBC_REQ="$(grep -aoE 'GLIBC_2\.[0-9]+' "$PYLIB" 2>/dev/null |
                 sed 's/GLIBC_//' | sort -V | tail -1)"
    log "glibc: system=$GLIBC_SYS required=${GLIBC_REQ:-?}"
    if [ -n "$GLIBC_REQ" ] && ! ver_le "$GLIBC_REQ" "$GLIBC_SYS"; then
        die "This package was built for a newer Linux release than this machine.

  glibc required by the package : $GLIBC_REQ
  glibc installed on this system: $GLIBC_SYS

glibc is not forward compatible, so the package cannot run here as-is.
It must be rebuilt on a system with glibc $GLIBC_SYS or older — ideally on
this very machine:

  ./build_linux.sh"
    fi
fi

# --------------------------------------------- 3. Qt platform + system libs --
# Pick the platform plugin unless the user pinned one.  xcb is preferred: it
# works natively on X11 and through XWayland on Wayland sessions, and it is the
# best-tested path for this Qt5 build.
if [ -z "${QT_QPA_PLATFORM:-}" ]; then
    if [ -n "${DISPLAY:-}" ]; then
        export QT_QPA_PLATFORM=xcb
    elif [ -n "${WAYLAND_DISPLAY:-}" ]; then
        export QT_QPA_PLATFORM=wayland
    else
        die "No graphical session found (neither DISPLAY nor WAYLAND_DISPLAY is set).

ONTbarcoder needs a desktop session. If you connected over SSH,
enable X11 forwarding:
  ssh -X user@host"
    fi
fi
log "QT_QPA_PLATFORM=$QT_QPA_PLATFORM"

# The bundle does not ship every libxcb-* dependency of libqxcb.so; those come
# from the distribution.  On a minimal Ubuntu they are missing and Qt aborts
# before any window appears.
if [ "$QT_QPA_PLATFORM" = "xcb" ] && [ -f "$QXCB" ] && command -v ldd >/dev/null 2>&1; then
    missing="$(LD_LIBRARY_PATH="$APP_DIR/_internal:${LD_LIBRARY_PATH:-}" \
               ldd "$QXCB" 2>/dev/null | awk '/not found/ {print $1}' | sort -u)"
    if [ -n "$missing" ]; then
        pkgs=""
        while IFS= read -r so; do
            case "$so" in
                libxcb-icccm*)       pkgs="$pkgs libxcb-icccm4" ;;
                libxcb-image*)       pkgs="$pkgs libxcb-image0" ;;
                libxcb-keysyms*)     pkgs="$pkgs libxcb-keysyms1" ;;
                libxcb-randr*)       pkgs="$pkgs libxcb-randr0" ;;
                libxcb-render-util*) pkgs="$pkgs libxcb-render-util0" ;;
                libxcb-shape*)       pkgs="$pkgs libxcb-shape0" ;;
                libxcb-sync*)        pkgs="$pkgs libxcb-sync1" ;;
                libxcb-util*)        pkgs="$pkgs libxcb-util1" ;;
                libxcb-xfixes*)      pkgs="$pkgs libxcb-xfixes0" ;;
                libxcb-xinerama*)    pkgs="$pkgs libxcb-xinerama0" ;;
                libxcb-xkb*)         pkgs="$pkgs libxcb-xkb1" ;;
                libxcb-cursor*)      pkgs="$pkgs libxcb-cursor0" ;;
                libxcb.so*)          pkgs="$pkgs libxcb1" ;;
                libxkbcommon-x11*)   pkgs="$pkgs libxkbcommon-x11-0" ;;
                libxkbcommon.so*)    pkgs="$pkgs libxkbcommon0" ;;
                libX11-xcb*)         pkgs="$pkgs libx11-xcb1" ;;
            esac
        done <<<"$missing"
        pkgs="$(printf '%s\n' $pkgs | sort -u | tr '\n' ' ')"
        [ -n "${pkgs// /}" ] || pkgs="libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-util1 libxcb-xkb1"
        die "Some system libraries Qt needs to open a window are missing:

$(printf '  %s\n' $missing)

Install them with:
  sudo apt install $pkgs"
    fi
fi

# ------------------------------------------------------------------ 4. run --
# Work from the bundle directory: the app resolves _mafftfiles/, guide/ and
# _profiles/ relative to the executable.
cd "$APP_DIR" || die "Could not enter $APP_DIR"

log "exec $BIN $*"
if [ -t 1 ]; then
    "$BIN" "$@" 2>&1 | tee -a "$LOG"
    rc=${PIPESTATUS[0]}
else
    "$BIN" "$@" >>"$LOG" 2>&1
    rc=$?
fi
log "exit code: $rc"

# Exit code 0 = clean quit.  Anything else died before or during the GUI, and
# without this dialog the user would again see nothing at all.
if [ "$rc" -ne 0 ]; then
    tail_txt="$(tail -n 20 "$LOG" 2>/dev/null)"
    crash="$HOME/ONTbarcoder_crash.log"
    extra=""
    [ -f "$crash" ] && extra=$'\n\n'"Application traceback: $crash"
    show_error "ONTbarcoder 3 stopped unexpectedly (exit code $rc).

Last lines of the log:
$tail_txt

Full log: $LOG$extra"
    exit "$rc"
fi
