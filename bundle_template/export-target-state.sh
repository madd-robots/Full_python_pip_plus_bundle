#!/usr/bin/env bash
# export-target-state.sh - run on the MX Linux machine (no root, no network,
# read-only). Captures what is installed so the bundle can be built to match
# this exact machine, including any base packages that must be updated together.
#
#   ./export-target-state.sh [OUTPUT_DIR]      (default: ./mx-target-state)
#
# Copy OUTPUT_DIR to the build device (USB stick, SD card) and build with:
#   ./build_madd_python_bundle.sh --target-state OUTPUT_DIR
#
# The directory contains the dpkg package database (package names, versions,
# descriptions), os-release, architecture and MX version. No user files, keys,
# or network configuration are copied.
set -Eeuo pipefail
umask 022
PATH=/usr/bin:/bin
export PATH LC_ALL=C

OUT=${1:-./mx-target-state}
die() { printf 'export-target-state.sh: %s\n' "$*" >&2; exit 1; }

[ ! -e "$OUT" ] || die "$OUT already exists; choose a new directory name"
[ -r /var/lib/dpkg/status ] || die "/var/lib/dpkg/status not readable"
command -v dpkg >/dev/null 2>&1 || die "dpkg not found"

mkdir -p -- "$OUT"
cp -- /var/lib/dpkg/status "$OUT/status"
cp -- /etc/os-release "$OUT/os-release"
[ ! -r /etc/debian_version ] || cp -- /etc/debian_version "$OUT/debian_version"
[ ! -r /etc/mx-version ] || cp -- /etc/mx-version "$OUT/mx-version"
dpkg --print-architecture >"$OUT/dpkg-architecture"
uname -m >"$OUT/uname-machine"
date -u +%Y-%m-%dT%H:%M:%SZ >"$OUT/exported-utc"
( cd -- "$OUT" && sha256sum -- * >SHA256SUMS )

printf 'Target state written to %s (%s installed-package records).\n' \
    "$OUT" "$(grep -c '^Package: ' "$OUT/status")"
printf 'Copy that directory to the build device and run:\n'
printf '    ./build_madd_python_bundle.sh --target-state DIR\n'
