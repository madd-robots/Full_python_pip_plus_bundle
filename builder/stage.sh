#!/usr/bin/env bash
# stage.sh - entry point of the Debian package (APT) stage.
# Runs on a Debian/Ubuntu host, or inside the existing ubuntu-claude-secure
# proot-distro environment when the builder runs in Termux. Checks the APT-stage
# prerequisites, then runs madd_debstage.py (no root needed for the stage itself).
# Invoked by builder/madd_stage.py; not meant to be run by hand.
set -Eeuo pipefail
umask 022
export LC_ALL=C

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
INSTALL_PREREQS=0
CHECK_ONLY=0
PROFILE=""
ARGS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-staging-prereqs) INSTALL_PREREQS=1 ;;
        --check-prereqs-only) CHECK_ONLY=1 ;;
        --profile)
            [ "$#" -ge 2 ] || { printf 'stage.sh: --profile needs a value\n' >&2; exit 2; }
            PROFILE=$2; ARGS+=("$1" "$2"); shift ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

say() { printf '[stage] %s\n' "$*"; }
die() { printf '[stage] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$PROFILE" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "missing or invalid --profile"
PROFILE_FILE="$HERE/profiles/$PROFILE.conf"
[ -f "$PROFILE_FILE" ] || die "no such profile: $PROFILE_FILE"
prof() { sed -n "s/^$1=//p" "$PROFILE_FILE" | head -n 1; }
KEYRING=$(prof STAGING_KEYRING)
KEYRING_PKG=$(prof STAGING_KEYRING_PACKAGE)

[ -r /etc/os-release ] || die "APT stage environment has no /etc/os-release"
OS_ID=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
OS_LIKE=$(sed -n 's/^ID_LIKE=//p' /etc/os-release | tr -d '"')
OS_PRETTY=$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"')
case " $OS_ID $OS_LIKE " in
    *" ubuntu "*|*" debian "*) ;;
    *) die "the APT stage needs Ubuntu or Debian (found: $OS_PRETTY)" ;;
esac
say "APT stage environment: $OS_PRETTY, $(uname -m)"

for cmd in apt-get apt-cache dpkg-deb; do
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found: the APT stage needs a working APT/dpkg"
done

missing=()
command -v python3 >/dev/null 2>&1 || missing+=(python3)
if ! command -v gpgv >/dev/null 2>&1 && ! command -v sqv >/dev/null 2>&1; then
    missing+=(gpgv)
fi
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || missing+=("python3>=3.9")
fi
PREREQ_PKGS=(python3 gpgv ca-certificates)
[ -f "$KEYRING" ] || PREREQ_PKGS+=("$KEYRING_PKG")

if [ "${#missing[@]}" -gt 0 ]; then
    if [ "$INSTALL_PREREQS" -ne 1 ]; then
        say "missing APT-stage prerequisites: ${missing[*]}"
        say "install them inside the APT stage environment with:"
        say "    apt-get update && apt-get install --no-install-recommends ${PREREQ_PKGS[*]}"
        die "or rerun the builder with --install-staging-prereqs"
    fi
fi
if [ "$INSTALL_PREREQS" -eq 1 ] && { [ "${#missing[@]}" -gt 0 ] || [ ! -f "$KEYRING" ]; }; then
    [ "$(id -u)" -eq 0 ] || die "--install-staging-prereqs needs root inside the APT stage environment"
    say "installing APT-stage prerequisites (this environment only): ${PREREQ_PKGS[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PREREQ_PKGS[@]}"
fi
[ -f "$KEYRING" ] || say "note: $KEYRING not installed; the stage will fetch $KEYRING_PKG through this system's signed APT sources"

if [ "$CHECK_ONLY" -eq 1 ]; then
    say "APT-stage prerequisites: OK"
    exit 0
fi
exec python3 "$HERE/madd_debstage.py" "${ARGS[@]}"
