#!/usr/bin/env bash
# build_madd_python_bundle.sh - build the offline MX Linux Python bundle.
#
# From Termux on Android (default): delegates all staging work into the
# existing proot-distro Ubuntu environment "ubuntu-claude-secure".
# From a Debian/Ubuntu Linux host: pass --staging local to use that host.
#
# Output: OUTPUT/dist/madd-python-full-mx25-amd64.tar.gz (+ .sha256, build
# report, wireless audit). A previous bundle is replaced only after the new
# one has passed every build-time test.
set -Eeuo pipefail
umask 022
export LC_ALL=C

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
STAGING=auto
DISTRO=ubuntu-claude-secure
OUTPUT_DIR="$PROJECT_DIR/output"
PROFILE=mx25-amd64
MIN_FREE_GB=4
PASS_ARGS=()

usage() {
    cat <<EOF
Usage: ./build_madd_python_bundle.sh [options]

  --staging auto|proot|local   auto = proot in Termux; local = run on this
                               Debian/Ubuntu host (default: auto)
  --distro NAME                proot-distro environment (default: $DISTRO)
  --output DIR                 output directory (default: ./output)
  --profile ID                 target profile in builder/profiles (default: $PROFILE)
  --min-free-gb N              required free space in GiB (default: $MIN_FREE_GB)
  --install-staging-prereqs    apt-get install the staging tools inside the
                               staging environment (never on the target)
  --keep-staging               keep the staging directory if the build fails
  -h, --help                   this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --staging) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; STAGING=$2; shift ;;
        --distro) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; DISTRO=$2; shift ;;
        --output) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; OUTPUT_DIR=$2; shift ;;
        --profile) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; PROFILE=$2; shift ;;
        --min-free-gb) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; MIN_FREE_GB=$2; shift ;;
        --install-staging-prereqs|--keep-staging) PASS_ARGS+=("$1") ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '[build] %s\n' "$*"; }
die() { printf '[build] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$PROFILE" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid profile name: $PROFILE"
[[ "$DISTRO" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid distro name: $DISTRO"
[[ "$MIN_FREE_GB" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "invalid --min-free-gb: $MIN_FREE_GB"
PROFILE_FILE="$PROJECT_DIR/builder/profiles/$PROFILE.conf"
[ -f "$PROFILE_FILE" ] || die "no such profile: $PROFILE_FILE"
TARGET_ARCH=$(sed -n 's/^DEB_ARCH=//p' "$PROFILE_FILE")
TARGET_NAME=$(sed -n 's/^TARGET_DISTRIBUTION=//p' "$PROFILE_FILE")

IS_TERMUX=0
if [ -n "${TERMUX_VERSION:-}" ]; then
    IS_TERMUX=1
else
    case "${PREFIX:-}" in */com.termux/*) IS_TERMUX=1 ;; esac
fi
HOST_ARCH=$(uname -m)
say "host: $(uname -s) $HOST_ARCH$( [ "$IS_TERMUX" -eq 1 ] && printf ' (Termux)')"
say "target profile: $PROFILE -> $TARGET_NAME, Debian architecture $TARGET_ARCH (explicit; independent of host)"

if [ "$STAGING" = auto ]; then
    if [ "$IS_TERMUX" -eq 1 ]; then
        STAGING=proot
    else
        die "not running in Termux. To use this Linux host itself as the staging environment, pass --staging local"
    fi
fi

# Storage check on the output location (the staging side re-checks too).
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR=$(cd -- "$OUTPUT_DIR" && pwd -P)
avail_kb=$(df -Pk -- "$OUTPUT_DIR" | awk 'NR == 2 { print $4 }')
need_kb=$(awk -v g="$MIN_FREE_GB" 'BEGIN { printf "%d", g * 1048576 }')
say "free space at $OUTPUT_DIR: $((avail_kb / 1024)) MiB (required $((need_kb / 1024)) MiB)"
[ "$avail_kb" -ge "$need_kb" ] || die "insufficient storage: free at least ${MIN_FREE_GB} GiB or use --output on a larger volume"

STAGE_ARGS=(--profile "$PROFILE" --min-free-gb "$MIN_FREE_GB" "${PASS_ARGS[@]}")

case "$STAGING" in
    proot)
        [ "$IS_TERMUX" -eq 1 ] || die "--staging proot is only supported from Termux"
        command -v proot-distro >/dev/null 2>&1 || die "proot-distro not found (pkg install proot-distro)"
        ROOTFS="${PREFIX:-/data/data/com.termux/files/usr}/var/lib/proot-distro/installed-rootfs/$DISTRO"
        if [ ! -d "$ROOTFS" ]; then
            say "installed proot-distro environments:"
            proot-distro list 2>&1 | sed 's/^/    /' || true
            die "proot-distro environment '$DISTRO' not found at $ROOTFS"
        fi
        say "using existing proot-distro environment: $DISTRO"
        proot-distro login "$DISTRO" -- /bin/true || die "cannot start '$DISTRO' (proot-distro login failed)"
        STAGING_OS=$(proot-distro login "$DISTRO" -- /bin/sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"')
        say "staging OS: ${STAGING_OS:-unknown}"
        MNT_PROJECT=/mnt/madd-python-project
        MNT_OUTPUT=/mnt/madd-python-output
        proot-distro login "$DISTRO" \
            --bind "$PROJECT_DIR:$MNT_PROJECT" \
            --bind "$OUTPUT_DIR:$MNT_OUTPUT" \
            -- /usr/bin/env MADD_STAGING_LABEL="proot-distro $DISTRO on $HOST_ARCH" \
            /bin/bash "$MNT_PROJECT/builder/stage.sh" --output "$MNT_OUTPUT" "${STAGE_ARGS[@]}"
        ;;
    local)
        [ "$IS_TERMUX" -eq 0 ] || die "Termux itself is not a valid staging environment; use --staging proot"
        MADD_STAGING_LABEL="local host $HOST_ARCH" \
            bash "$PROJECT_DIR/builder/stage.sh" --output "$OUTPUT_DIR" "${STAGE_ARGS[@]}"
        ;;
    *) die "invalid --staging value: $STAGING" ;;
esac

BUNDLE_NAME=$(sed -n 's/^BUNDLE_NAME=//p' "$PROFILE_FILE")
FINAL="$OUTPUT_DIR/dist/$BUNDLE_NAME.tar.gz"
if [ ! -f "$FINAL" ] || [ ! -f "$FINAL.sha256" ]; then die "build finished without $FINAL"; fi
( cd -- "$OUTPUT_DIR/dist" && sha256sum -c -- "$BUNDLE_NAME.tar.gz.sha256" ) || die "final archive checksum mismatch"
say "final archive: $FINAL ($(du -h -- "$FINAL" | awk '{ print $1 }'))"
say "checksum:      $(cat -- "$FINAL.sha256")"
say "copy both $BUNDLE_NAME.tar.gz and $BUNDLE_NAME.tar.gz.sha256 to the MX machine."
