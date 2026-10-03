#!/usr/bin/env bash
# build_madd_python_bundle.sh - build the offline Python bundle.
#
# Termux on Android is the normal place to run this. Everything runs natively in
# Termux (Python wheel resolution for the target, verification, manifests,
# audits, ShellCheck, archive) except the Debian package (APT) stage, which runs
# inside the existing proot-distro environment "ubuntu-claude-secure" because
# resolving Debian dependencies needs APT. On a Debian/Ubuntu host (including
# inside that proot environment) every stage runs locally.
#
# Output: OUTPUT/dist/<bundle>.tar.gz (+ .sha256, build report, inventory,
# wireless audit). A previous bundle is replaced only after the new one has
# passed every build-time test.
set -Eeuo pipefail
umask 022
export LC_ALL=C

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DISTRO=ubuntu-claude-secure
OUTPUT_DIR="$PROJECT_DIR/output"
PROFILE=mx25-amd64
TARGET_ARCH=""
MIN_FREE_GB=4
TARGET_STATE=""
ARCHIVE_KEYRING=""
INSTALL_PREREQS=0
PREFLIGHT_ONLY=0
PASS_ARGS=()

usage() {
    cat <<EOF
Usage: ./build_madd_python_bundle.sh [options]

  --profile ID                 target profile in builder/profiles (default: $PROFILE)
                               mx25-amd64: MX Linux 25 / Debian 13; ubuntu2404-amd64: Ubuntu 24.04
  --target-arch ARCH           expected target architecture (amd64/x86_64); must match the profile
  --target-state DIR           resolve against the state exported on the target machine by
                               export-target-state.sh (recommended for an exact match)
  --distro NAME                proot-distro environment for the APT stage when running in
                               Termux (default: $DISTRO)
  --output DIR                 output directory (default: ./output)
  --min-free-gb N              required free space in GiB (default: $MIN_FREE_GB)
  --install-staging-prereqs    install missing builder tools (Termux: pkg; Debian/Ubuntu: apt-get)
                               and the APT-stage tools inside the APT-stage environment
  --preflight-only             detect the environment and check prerequisites, then stop
  --keep-staging               keep the staging directory if the build fails
  --skip-baseline-model        do not model un-updated target machines (not recommended)
  --archive-keyring FILE       Debian/Ubuntu archive keyring for the APT stage (local stage only)
  -h, --help                   this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --profile) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; PROFILE=$2; shift ;;
        --target-arch) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; TARGET_ARCH=$2; shift ;;
        --target-state) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; TARGET_STATE=$2; shift ;;
        --distro) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; DISTRO=$2; shift ;;
        --output) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; OUTPUT_DIR=$2; shift ;;
        --min-free-gb) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; MIN_FREE_GB=$2; shift ;;
        --archive-keyring) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; ARCHIVE_KEYRING=$2; shift ;;
        --install-staging-prereqs) INSTALL_PREREQS=1 ;;
        --preflight-only) PREFLIGHT_ONLY=1 ;;
        --keep-staging|--skip-baseline-model) PASS_ARGS+=("$1") ;;
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
prof() { sed -n "s/^$1=//p" "$PROFILE_FILE" | head -n 1; }
PROFILE_ARCH=$(prof DEB_ARCH)
TARGET_NAME=$(prof TARGET_DISTRIBUTION)

# --- 1. builder environment -------------------------------------------------
under_proot() {
    local tracer comm
    tracer=$(sed -n 's/^TracerPid:[[:space:]]*//p' /proc/self/status 2>/dev/null || true)
    [ -n "$tracer" ] && [ "$tracer" != 0 ] || return 1
    comm=$(cat "/proc/$tracer/comm" 2>/dev/null || true)
    case "$comm" in proot*) return 0 ;; esac
    return 1
}
ENV_KIND=""
if [ -n "${TERMUX_VERSION:-}" ]; then
    ENV_KIND=termux
else
    case "${PREFIX:-}" in */com.termux/*) ENV_KIND=termux ;; esac
fi
if [ -z "$ENV_KIND" ]; then
    [ -r /etc/os-release ] || die "unsupported builder environment (not Termux, no /etc/os-release)"
    OS_ID=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
    OS_LIKE=$(sed -n 's/^ID_LIKE=//p' /etc/os-release | tr -d '"')
    OS_PRETTY=$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"')
    case " $OS_ID $OS_LIKE " in
        *" ubuntu "*|*" debian "*) ;;
        *) die "unsupported builder environment: $OS_PRETTY (use Termux or Debian/Ubuntu)" ;;
    esac
    if under_proot; then ENV_KIND=proot; else ENV_KIND=debian-host; fi
fi
HOST_ARCH=$(uname -m)
case "$ENV_KIND" in
    termux) ENV_LABEL="Termux ${TERMUX_VERSION:-} on Android, $HOST_ARCH"; DEB_STAGE="proot:$DISTRO" ;;
    proot) ENV_LABEL="$OS_PRETTY under proot, $HOST_ARCH"; DEB_STAGE=local ;;
    debian-host) ENV_LABEL="$OS_PRETTY, $HOST_ARCH"; DEB_STAGE=local ;;
esac
say "builder environment: $ENV_LABEL"

# --- 2./3. builder vs. target architecture ---------------------------------
case "$TARGET_ARCH" in
    "") ;;
    x86_64|amd64) TARGET_ARCH=amd64 ;;
    aarch64|arm64) TARGET_ARCH=arm64 ;;
    *) die "unsupported --target-arch: $TARGET_ARCH" ;;
esac
if [ -n "$TARGET_ARCH" ] && [ "$TARGET_ARCH" != "$PROFILE_ARCH" ]; then
    die "profile $PROFILE targets $PROFILE_ARCH, not $TARGET_ARCH (available profiles: $(find "$PROJECT_DIR/builder/profiles" -maxdepth 1 -name '*.conf' -printf '%f ' | sed 's/\.conf//g'))"
fi
say "target: $TARGET_NAME, Debian architecture $PROFILE_ARCH (profile $PROFILE)"
case "$HOST_ARCH:$PROFILE_ARCH" in
    x86_64:amd64|aarch64:arm64) say "builder and target architecture match" ;;
    *) say "cross-architecture build: target artifacts are selected explicitly for $PROFILE_ARCH and never executed here" ;;
esac

# --- 4. prerequisites -------------------------------------------------------
NEED_CMDS=(python3 shellcheck bash tar gzip sha256sum find awk sed grep sort comm cmp df)
if [ "$ENV_KIND" = termux ]; then
    NEED_CMDS+=(proot-distro)
else
    NEED_CMDS+=(apt-get apt-cache dpkg-deb)
fi
missing=()
for cmd in "${NEED_CMDS[@]}"; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys, ssl, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null ||
        missing+=("python3>=3.9-with-ssl-and-venv")
fi
if [ "${#missing[@]}" -gt 0 ]; then
    if [ "$ENV_KIND" = termux ]; then
        INSTALL_CMD=(pkg install -y python shellcheck proot-distro coreutils findutils diffutils gawk grep sed tar gzip)
    else
        INSTALL_CMD=(apt-get install -y --no-install-recommends python3 shellcheck coreutils findutils diffutils gawk tar gzip)
    fi
    say "missing builder prerequisites: ${missing[*]}"
    say "ShellCheck is a mandatory release gate; the build does not run without it."
    if [ "$INSTALL_PREREQS" -ne 1 ]; then
        say "install them with:"
        say "    ${INSTALL_CMD[*]}"
        die "or rerun with --install-staging-prereqs"
    fi
    if [ "$ENV_KIND" != termux ]; then
        [ "$(id -u)" -eq 0 ] || die "--install-staging-prereqs needs root on a Debian/Ubuntu host"
        DEBIAN_FRONTEND=noninteractive apt-get update
        DEBIAN_FRONTEND=noninteractive "${INSTALL_CMD[@]}"
    else
        "${INSTALL_CMD[@]}"
    fi
    for cmd in "${NEED_CMDS[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || die "still missing after installation: $cmd"
    done
fi
say "builder prerequisites: OK ($(python3 --version 2>&1), $(shellcheck --version | sed -n 's/^version: /shellcheck /p'))"

if [ "$ENV_KIND" = termux ]; then
    if ! proot-distro login "$DISTRO" -- /bin/true; then
        say "installed proot-distro environments:"
        proot-distro list 2>&1 | sed 's/^/    /' || true
        die "cannot start the proot-distro environment '$DISTRO' (needed for the APT stage only)"
    fi
    say "APT stage will run in proot-distro environment: $DISTRO"
    STAGE_CHECK=(proot-distro login "$DISTRO" --bind "$PROJECT_DIR:/mnt/madd-python-project" --
        /bin/bash /mnt/madd-python-project/builder/stage.sh --profile "$PROFILE" --check-prereqs-only)
else
    say "APT stage will run locally"
    STAGE_CHECK=(bash "$PROJECT_DIR/builder/stage.sh" --profile "$PROFILE" --check-prereqs-only)
fi
[ "$INSTALL_PREREQS" -ne 1 ] || STAGE_CHECK+=(--install-staging-prereqs)
"${STAGE_CHECK[@]}" || die "APT-stage prerequisites are not met (see above)"

# --- storage ----------------------------------------------------------------
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR=$(cd -- "$OUTPUT_DIR" && pwd -P)
avail_kb=$(df -Pk -- "$OUTPUT_DIR" | awk 'NR == 2 { print $4 }')
need_kb=$(awk -v g="$MIN_FREE_GB" 'BEGIN { printf "%d", g * 1048576 }')
say "free space at $OUTPUT_DIR: $((avail_kb / 1024)) MiB (required $((need_kb / 1024)) MiB)"
[ "$avail_kb" -ge "$need_kb" ] || die "insufficient storage: free at least ${MIN_FREE_GB} GiB or use --output on a larger volume"

if [ -n "$TARGET_STATE" ]; then
    [ -f "$TARGET_STATE/status" ] || die "--target-state $TARGET_STATE has no status file (see export-target-state.sh)"
    TARGET_STATE=$(cd -- "$TARGET_STATE" && pwd -P)
    if [ -f "$TARGET_STATE/SHA256SUMS" ]; then
        ( cd -- "$TARGET_STATE" && sha256sum --check --strict --quiet SHA256SUMS ) || die "target state checksum mismatch"
    fi
    say "target state: $TARGET_STATE (exported $(cat -- "$TARGET_STATE/exported-utc" 2>/dev/null || printf 'unknown'))"
    PASS_ARGS+=(--target-state "$TARGET_STATE")
else
    say "no --target-state given: generic resolution for $TARGET_NAME"
fi
if [ -n "$ARCHIVE_KEYRING" ]; then
    [ "$DEB_STAGE" = local ] || die "--archive-keyring is only supported when the APT stage runs locally"
    PASS_ARGS+=(--archive-keyring "$ARCHIVE_KEYRING")
fi
[ "$INSTALL_PREREQS" -ne 1 ] || PASS_ARGS+=(--install-staging-prereqs)

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
    say "preflight complete: environment and prerequisites OK; nothing was built (--preflight-only)"
    exit 0
fi

# --- 5.-13. build -------------------------------------------------------------
python3 "$PROJECT_DIR/builder/madd_stage.py" --output "$OUTPUT_DIR" --profile "$PROFILE" \
    --min-free-gb "$MIN_FREE_GB" --deb-stage "$DEB_STAGE" --builder-env "$ENV_LABEL" "${PASS_ARGS[@]}"

BUNDLE_NAME=$(prof BUNDLE_NAME)
FINAL="$OUTPUT_DIR/dist/$BUNDLE_NAME.tar.gz"
if [ ! -f "$FINAL" ] || [ ! -f "$FINAL.sha256" ]; then die "build finished without $FINAL"; fi
( cd -- "$OUTPUT_DIR/dist" && sha256sum -c -- "$BUNDLE_NAME.tar.gz.sha256" ) || die "final archive checksum mismatch"
say "final archive: $FINAL ($(du -h -- "$FINAL" | awk '{ print $1 }'))"
say "copy both $BUNDLE_NAME.tar.gz and $BUNDLE_NAME.tar.gz.sha256 to the target machine."
