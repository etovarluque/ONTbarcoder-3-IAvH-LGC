#!/usr/bin/env bash
#
# Build the ONTbarcoder3 Linux bundle inside a container running an OLD Ubuntu,
# so the result also runs on newer ones.
#
# Why: glibc is backward compatible but never forward compatible.  A bundle
# built on Ubuntu 24.04 (glibc 2.39) dies on Ubuntu 22.04 (glibc 2.35) with
# "version `GLIBC_2.38' not found".  Building against the oldest glibc you
# intend to support makes one bundle work everywhere from that release upward.
#
#   ./build_linux_docker.sh              # default: Ubuntu 22.04 -> glibc 2.35
#   ./build_linux_docker.sh 20.04        # even wider reach -> glibc 2.31
#
# Requires docker (or podman) on the build machine.  The source tree is mounted
# into the container, so dist/ appears here as usual when it finishes.
#
set -euo pipefail
cd "$(dirname "$0")"

UBUNTU_VERSION="${1:-22.04}"

if command -v docker >/dev/null 2>&1; then
    ENGINE=docker
elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
else
    echo "ERROR: neither docker nor podman is installed." >&2
    echo "Install docker, or run ./build_linux.sh directly on an Ubuntu $UBUNTU_VERSION machine." >&2
    exit 1
fi

echo "==> Building inside ubuntu:$UBUNTU_VERSION using $ENGINE"

# The build venv is bound to the container's Python, and PyInstaller caches
# host-specific paths — start both from scratch so a previous host build cannot
# leak in.
rm -rf .build-venv build dist

$ENGINE run --rm -t \
    -v "$PWD":/src \
    -w /src \
    -e DEBIAN_FRONTEND=noninteractive \
    "ubuntu:$UBUNTU_VERSION" \
    bash -euo pipefail -c '
        echo "--- installing build toolchain"
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            python3 python3-venv python3-dev python3-pip \
            build-essential ca-certificates binutils file >/dev/null

        echo "--- running build_linux.sh"
        chmod +x build_linux.sh
        ./build_linux.sh
    '

# Files created inside the container are owned by root when using docker.
if [ "$ENGINE" = "docker" ] && [ -d dist ]; then
    echo "==> Restoring ownership of build outputs"
    sudo chown -R "$(id -u):$(id -g)" .build-venv build dist 2>/dev/null || \
        echo "    (skipped — run: sudo chown -R \$USER:\$USER dist build .build-venv)"
fi

echo
echo "Done.  dist/ONTbarcoder3_linux.tar.gz was built against Ubuntu $UBUNTU_VERSION,"
echo "so it runs on Ubuntu $UBUNTU_VERSION and every newer release."
