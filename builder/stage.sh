#!/usr/bin/env bash
# stage.sh - entry point INSIDE the Ubuntu staging environment.
# Checks staging prerequisites, then runs madd_stage.py (no root needed for
# the build itself). Invoked by build_madd_python_bundle.sh.
set -Eeuo pipefail
umask 022
export LC_ALL=C

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
INSTALL_PREREQS=0
ARGS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-staging-prereqs) INSTALL_PREREQS=1 ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

say() { printf '[stage] %s\n' "$*"; }
die() { printf '[stage] ERROR: %s\n' "$*" >&2; exit 1; }

[ -r /etc/os-release ] || die "staging environment has no /etc/os-release"
OS_ID=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
OS_LIKE=$(sed -n 's/^ID_LIKE=//p' /etc/os-release | tr -d '"')
OS_PRETTY=$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"')
case " $OS_ID $OS_LIKE " in
    *" ubuntu "*|*" debian "*) ;;
    *) die "staging environment must be Ubuntu or Debian (found: $OS_PRETTY)" ;;
esac
say "staging environment: $OS_PRETTY, $(uname -m)"

PREREQ_PKGS=(python3 python3-venv gpgv ca-certificates debian-archive-keyring shellcheck)

for cmd in apt-get dpkg-deb; do
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found: the staging environment needs a working APT/dpkg"
done

missing=()
for cmd in python3 tar gzip sha256sum find; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if ! command -v gpgv >/dev/null 2>&1 && ! command -v sqv >/dev/null 2>&1; then
    missing+=(gpgv)
fi
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || missing+=("python3>=3.9")
    python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || missing+=(python3-venv)
fi

if [ "${#missing[@]}" -gt 0 ] || { [ "$INSTALL_PREREQS" -eq 1 ] && ! command -v shellcheck >/dev/null 2>&1; }; then
    if [ "$INSTALL_PREREQS" -ne 1 ]; then
        say "missing staging prerequisites: ${missing[*]}"
        say "install them inside the staging environment with:"
        say "    apt-get update && apt-get install --no-install-recommends ${PREREQ_PKGS[*]}"
        die "or rerun the builder with --install-staging-prereqs"
    fi
    [ "$(id -u)" -eq 0 ] || die "--install-staging-prereqs needs root inside the staging environment"
    say "installing staging prerequisites (staging environment only): ${PREREQ_PKGS[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PREREQ_PKGS[@]}"
fi

exec python3 "$HERE/madd_stage.py" "${ARGS[@]}"
