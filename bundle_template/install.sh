#!/usr/bin/env bash
# install.sh - offline installer for the MADD full Python toolchain bundle.
#
#   sudo ./install.sh                 preflight, confirm, install system packages
#                                     from the bundled local APT repo, then build
#                                     the user venv from the bundled wheelhouse
#   ./install.sh --preflight-only     checks only (no changes; works without root)
#   ./install.sh --skip-system        user venv only (system packages must already
#                                     be installed); run as the target user
#
# Makes no network connections: APT sees only the bundled file: repository and
# pip runs with --no-index. Does not touch /etc/apt sources, shell startup files,
# services, firewall, boot, or wireless/NetworkManager configuration.
set -Eeuo pipefail
umask 022
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH LC_ALL=C

BUNDLE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROG=install.sh

PREFLIGHT_ONLY=0
ASSUME_YES=0
SKIP_SYSTEM=0
NO_LAUNCHERS=0
TARGET_USER=""
VENV=""
WORK=""

usage() {
    cat <<EOF
Usage: sudo ./$PROG [options]

Options:
  --preflight-only     run every check (integrity, OS, arch, disk, APT simulation)
                       and stop before changing anything
  --yes                do not ask for confirmation before installing
  --skip-system        skip the APT phase; only build the user environment
                       (requires the system packages to be installed already)
  --target-user USER   user who owns the Python environment (default: \$SUDO_USER)
  --venv DIR           environment location (default: ~USER/.venvs/madd-python)
  --no-launchers       do not create madd-python / madd-pip / madd-python-env
  -h, --help           show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --preflight-only) PREFLIGHT_ONLY=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --skip-system) SKIP_SYSTEM=1 ;;
        --no-launchers) NO_LAUNCHERS=1 ;;
        --target-user) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; TARGET_USER=$2; shift ;;
        --venv) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; VENV=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf '%s: unknown option: %s\n' "$PROG" "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

info() { printf '[%s] %s\n' "$PROG" "$*"; }
warn() { printf '[%s] WARNING: %s\n' "$PROG" "$*" >&2; }
die() { printf '[%s] STOP: %s\n' "$PROG" "$*" >&2; exit 1; }
section() { printf '\n==== %s ====\n' "$*"; }

on_error() {
    printf '[%s] STOP: unexpected failure at line %s (exit %s); no further steps were run.\n' \
        "$PROG" "$1" "$2" >&2
}
trap 'on_error "$LINENO" "$?"' ERR

cleanup() {
    # WORK is only ever set to a directory this script created with mktemp.
    if [ -n "$WORK" ] && [ -d "$WORK" ] && [ ! -L "$WORK" ]; then
        case "$WORK" in
            /var/tmp/madd-python-install.*|/tmp/madd-python-install.*)
                [ -f "$WORK/.madd-python-install" ] && rm -rf -- "$WORK" ;;
        esac
    fi
}
trap cleanup EXIT

kv() { sed -n "s/^$1=//p" "$2" | head -n 1; }

# --------------------------------------------------------------------------
section "Preflight: bundle metadata"
for f in BUILD_INFO.txt profile.conf SHA256SUMS deb-install-list.txt requirements.lock verify.sh; do
    [ -f "$BUNDLE_DIR/$f" ] || die "bundle file missing: $f"
done
BI="$BUNDLE_DIR/BUILD_INFO.txt"
BUNDLE_NAME=$(kv BUNDLE_NAME "$BI")
WANT_ARCH=$(kv DEB_ARCH "$BI")
WANT_MACHINE=$(kv UNAME_MACHINE "$BI")
WANT_DEBIAN=$(kv DEBIAN_VERSION "$BI")
WANT_CODENAME=$(kv DEBIAN_CODENAME "$BI")
PY_SHORT=$(kv PYTHON_VERSION "$BI")
DEB_BYTES=$(kv DEB_TOTAL_BYTES "$BI")
DEB_KB=$(kv DEB_INSTALLED_SIZE_KB "$BI")
PYDIST_BYTES=$(kv PYDIST_TOTAL_BYTES "$BI")
SDIST_COUNT=$(kv SDIST_COUNT "$BI")
for v in BUNDLE_NAME WANT_ARCH WANT_MACHINE WANT_DEBIAN WANT_CODENAME PY_SHORT DEB_BYTES DEB_KB PYDIST_BYTES SDIST_COUNT; do
    [ -n "${!v}" ] || die "BUILD_INFO.txt is incomplete ($v)"
done
for v in DEB_BYTES DEB_KB PYDIST_BYTES SDIST_COUNT; do
    case "${!v}" in *[!0-9]*) die "BUILD_INFO.txt value $v is not numeric" ;; esac
done
mapfile -t DEB_PKGS < <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$BUNDLE_DIR/deb-install-list.txt")
[ "${#DEB_PKGS[@]}" -gt 0 ] || die "deb-install-list.txt is empty"
for p in "${DEB_PKGS[@]}"; do
    [[ "$p" =~ ^[a-z0-9][a-z0-9.+-]*$ ]] || die "invalid package name in deb-install-list.txt: $p"
done
info "bundle $BUNDLE_NAME: Debian $WANT_DEBIAN ($WANT_CODENAME) $WANT_ARCH, Python $PY_SHORT"

# --------------------------------------------------------------------------
section "Preflight: required commands"
NEED=(sha256sum awk sed grep sort find comm cmp df stat id getent mktemp cp uname dpkg dpkg-query)
[ "$SKIP_SYSTEM" -eq 1 ] || NEED+=(apt-get)
[ "$(id -u)" -ne 0 ] || NEED+=(runuser)
for cmd in "${NEED[@]}"; do
    command -v "$cmd" >/dev/null 2>&1 || die "required command not found: $cmd"
done
info "all required commands present"

# --------------------------------------------------------------------------
section "Preflight: operating system and architecture"
[ -r /etc/os-release ] || die "/etc/os-release not found; unsupported system"
osr() { sed -n "s/^$1=//p" /etc/os-release | head -n 1 | sed -e 's/^"//' -e 's/"$//'; }
OS_ID=$(osr ID)
OS_LIKE=$(osr ID_LIKE)
OS_VERSION_ID=$(osr VERSION_ID)
OS_CODENAME=$(osr VERSION_CODENAME)
DEB_VERSION_FILE=""
[ -r /etc/debian_version ] && DEB_VERSION_FILE=$(head -n 1 /etc/debian_version)
MX_VERSION=""
[ -r /etc/mx-version ] && MX_VERSION=$(head -n 1 /etc/mx-version)

case " $OS_ID $OS_LIKE " in
    *" debian "*) ;;
    *) die "not a Debian-family system (ID=$OS_ID); this bundle targets MX Linux 25 / Debian 13" ;;
esac
DEB_MAJOR=${DEB_VERSION_FILE%%.*}
if [ "$DEB_MAJOR" != "$WANT_DEBIAN" ] && [ "$OS_VERSION_ID" != "$WANT_DEBIAN" ]; then
    die "Debian base mismatch: /etc/debian_version='$DEB_VERSION_FILE' VERSION_ID='$OS_VERSION_ID'; bundle requires Debian $WANT_DEBIAN"
fi
if [ -n "$OS_CODENAME" ] && [ "$OS_CODENAME" != "$WANT_CODENAME" ]; then
    die "codename mismatch: system '$OS_CODENAME', bundle '$WANT_CODENAME'"
fi
if [ -n "$MX_VERSION" ]; then
    case "$MX_VERSION" in
        MX-25*) PROFILE_MATCH="MX Linux 25 ($MX_VERSION)" ;;
        *) die "unsupported MX release '$MX_VERSION'; this bundle is for MX Linux 25 (Debian $WANT_DEBIAN)" ;;
    esac
else
    # Explicit compatible profile: plain Debian 13 is MX 25's base distribution.
    PROFILE_MATCH="Debian $WANT_DEBIAN ($WANT_CODENAME) - compatible base of MX Linux 25"
fi
SYS_ARCH=$(dpkg --print-architecture)
SYS_MACHINE=$(uname -m)
[ "$SYS_ARCH" = "$WANT_ARCH" ] || die "architecture mismatch: system dpkg architecture '$SYS_ARCH', bundle '$WANT_ARCH'"
[ "$SYS_MACHINE" = "$WANT_MACHINE" ] || die "machine mismatch: uname -m '$SYS_MACHINE', bundle '$WANT_MACHINE'"
info "system: $PROFILE_MATCH, $SYS_ARCH / $SYS_MACHINE - supported"

# --------------------------------------------------------------------------
section "Preflight: user and permissions"
if [ "$(id -u)" -eq 0 ]; then
    [ -n "$TARGET_USER" ] || TARGET_USER=${SUDO_USER:-}
    [ -n "$TARGET_USER" ] || die "running as root without sudo: pass --target-user USER (the account that will own the Python environment)"
else
    CUR_USER=$(id -un)
    if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "$CUR_USER" ]; then
        die "--target-user other than yourself requires root"
    fi
    TARGET_USER=$CUR_USER
    if [ "$PREFLIGHT_ONLY" -eq 0 ] && [ "$SKIP_SYSTEM" -eq 0 ]; then
        die "system package installation needs root: run 'sudo ./$PROG' (or use --preflight-only / --skip-system)"
    fi
fi
[[ "$TARGET_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || die "invalid user name: $TARGET_USER"
PW=$(getent passwd "$TARGET_USER") || die "user not found: $TARGET_USER"
TARGET_UID=$(printf '%s' "$PW" | cut -d: -f3)
TARGET_HOME=$(printf '%s' "$PW" | cut -d: -f6)
if [ "$TARGET_UID" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$TARGET_USER" = root ]; then
    warn "installing the Python environment for root"
fi
[ -d "$TARGET_HOME" ] || die "home directory of $TARGET_USER does not exist: $TARGET_HOME"
[ -n "$VENV" ] || VENV="$TARGET_HOME/.venvs/madd-python"
case "$VENV" in /*) ;; *) die "--venv must be an absolute path" ;; esac
[[ "$VENV" =~ ^/[A-Za-z0-9._/+-]+$ ]] || die "venv path contains unsupported characters (use --venv with a plain path): $VENV"
case "/$VENV/" in */../*|*/./*) die "venv path must not contain . or .. components" ;; esac
if [ "$VENV" = "$TARGET_HOME" ] || [ "$VENV" = / ]; then die "refusing venv location $VENV"; fi
BIN_DIR="$TARGET_HOME/.local/bin"

# Run a command as the target user with a clean, offline environment.
as_user() {
    local envv=(HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER"
        PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C.UTF-8 LANG=C.UTF-8
        http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9
        HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 no_proxy= NO_PROXY=
        PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONNOUSERSITE=1
        PYTHONDONTWRITEBYTECODE=1)
    local v
    for v in DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR; do
        [ -z "${!v:-}" ] || envv+=("$v=${!v}")
    done
    if [ "$(id -u)" -eq 0 ] && [ "$TARGET_UID" -ne 0 ]; then
        runuser -u "$TARGET_USER" -- env -i "${envv[@]}" "$@"
    else
        env -i "${envv[@]}" "$@"
    fi
}

as_user test -r "$BUNDLE_DIR/requirements.lock" -a -x "$BUNDLE_DIR/payload/wheels" ||
    die "user $TARGET_USER cannot read the bundle at $BUNDLE_DIR; extract it somewhere that user can read"
info "target user: $TARGET_USER (home $TARGET_HOME), environment: $VENV"

# --------------------------------------------------------------------------
section "Preflight: bundle integrity"
if [ "$(id -u)" -eq 0 ]; then
    WORK=$(mktemp -d /var/tmp/madd-python-install.XXXXXXXX)
else
    WORK=$(mktemp -d /tmp/madd-python-install.XXXXXXXX)
fi
# Root-owned and not writable by others; world-readable so APT's _apt sandbox
# user can read the file: repository.
chmod 0755 "$WORK"
: >"$WORK/.madd-python-install"
# Snapshot the checksum list first; everything below is checked against this copy.
cp -- "$BUNDLE_DIR/SHA256SUMS" "$WORK/SHA256SUMS"
bash "$BUNDLE_DIR/verify.sh" --quiet --sums "$WORK/SHA256SUMS" || die "bundle integrity check failed (hash mismatch, missing or unexpected file)"
info "bundle integrity: PASS ($(wc -l <"$WORK/SHA256SUMS") files)"

# --------------------------------------------------------------------------
section "Preflight: disk space"
avail_kb() { df -Pk -- "$1" | awk 'NR == 2 { print $4 }'; }
need_space() {
    local path=$1 need_kb=$2 what=$3 have
    have=$(avail_kb "$path")
    if [ "$have" -lt "$need_kb" ]; then
        die "not enough space for $what on $path: need $((need_kb / 1024)) MiB, have $((have / 1024)) MiB"
    fi
    info "space for $what on $path: need $((need_kb / 1024)) MiB, have $((have / 1024)) MiB"
}
VENV_PARENT=$VENV
while [ ! -d "$VENV_PARENT" ]; do VENV_PARENT=$(dirname -- "$VENV_PARENT"); done
need_space "$VENV_PARENT" $((PYDIST_BYTES * 4 / 1024 + 262144)) "Python environment"
if [ "$SKIP_SYSTEM" -eq 0 ]; then
    need_space "$WORK" $((DEB_BYTES / 1024 + 65536)) "private copy of the package repository"
    need_space /usr $((DEB_KB + 262144)) "system packages (upper bound)"
    need_space /var/cache $((65536)) "APT/dpkg bookkeeping"
fi

# --------------------------------------------------------------------------
section "Preflight: current Python package state"
printf '  %-24s %-28s %s\n' PACKAGE INSTALLED BUNDLED
for p in "${DEB_PKGS[@]}"; do
    have=$(dpkg-query -W -f='${db:Status-Abbrev} ${Version}' "$p" 2>/dev/null || true)
    case "$have" in ii*) have=${have#ii } ; have=${have# } ;; *) have="-" ;; esac
    want=$(awk -v p="$p" '$1 == p { print $2 }' "$BUNDLE_DIR/deb-manifest.txt")
    printf '  %-24s %-28s %s\n' "$p" "$have" "${want:-(provided by dependency)}"
done
command -v python3 >/dev/null 2>&1 && info "current system python3: $(python3 --version 2>&1)"
if [ -e "$VENV" ]; then
    if [ -f "$VENV/.madd-python-bundle" ]; then
        info "existing environment from this project found at $VENV (will be updated in place)"
    else
        die "$VENV exists but was not created by this bundle; it will not be touched. Use --venv to choose another location."
    fi
fi

# --------------------------------------------------------------------------
APT_CONF="$WORK/apt.conf"
REPO="$WORK/repo"
apt_local() {
    # Every APT call uses only the private file: repository; system sources,
    # lists, pins and caches are left untouched.
    APT_LISTBUGS_FRONTEND=none APT_LISTCHANGES_FRONTEND=none DEBIAN_FRONTEND=noninteractive \
    http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 ftp_proxy=http://127.0.0.1:9 \
        apt-get -c "$APT_CONF" "$@"
}

if [ "$SKIP_SYSTEM" -eq 0 ]; then
    section "Preflight: private local APT repository"
    mkdir -p "$REPO" "$WORK/lists/partial" "$WORK/archives/partial" "$WORK/sources.list.d" "$WORK/preferences.d"
    # Copy the repository into a private directory and re-verify the copy, so
    # files in a user-writable bundle cannot be swapped after verification.
    cp -- "$BUNDLE_DIR"/payload/debs/* "$REPO/"
    awk '$2 ~ /^payload\/debs\// { sub(/^payload\/debs\//, "", $2); print $1 "  " $2 }' "$WORK/SHA256SUMS" >"$WORK/repo.sums"
    ( cd -- "$REPO" && sha256sum --check --strict --quiet -- "$WORK/repo.sums" ) ||
        die "private repository copy failed verification"
    [ "$(find "$REPO" -maxdepth 1 -type f | wc -l)" -eq "$(wc -l <"$WORK/repo.sums")" ] ||
        die "private repository copy contains unexpected files"
    printf 'deb [trusted=yes] file:%s ./\n' "$REPO" >"$WORK/sources.list"
    cat >"$APT_CONF" <<EOF
Dir::Etc::sourcelist "$WORK/sources.list";
Dir::Etc::sourceparts "$WORK/sources.list.d/";
Dir::Etc::preferences "$WORK/preferences";
Dir::Etc::preferencesparts "$WORK/preferences.d/";
Dir::State::lists "$WORK/lists/";
Dir::Cache::archives "$WORK/archives/";
Dir::Cache::pkgcache "";
Dir::Cache::srcpkgcache "";
APT::Install-Recommends "false";
APT::Install-Suggests "false";
APT::Get::List-Cleanup "false";
Acquire::Languages "none";
Acquire::http::Proxy "http://127.0.0.1:9";
Acquire::https::Proxy "http://127.0.0.1:9";
#clear APT::Update::Pre-Invoke;
#clear APT::Update::Post-Invoke;
#clear APT::Update::Post-Invoke-Success;
EOF
    apt_local -q update >"$WORK/update.log" 2>&1 || { cat "$WORK/update.log" >&2; die "could not read the local repository"; }
    if grep -E '^(Err|W:|E:)' "$WORK/update.log" >&2; then
        die "local repository index produced errors/warnings"
    fi
    info "local repository: $(grep -c '^Package: ' "$REPO/Packages") packages indexed, no network sources configured"

    section "Preflight: dependency resolution simulation (no changes)"
    sim_rc=0
    apt_local -s -q --no-install-recommends --no-remove install "${DEB_PKGS[@]}" >"$WORK/sim.log" 2>&1 || sim_rc=$?
    if [ "$sim_rc" -ne 0 ]; then
        sed -n '1,80p' "$WORK/sim.log" >&2
        die "dependency resolution failed: required packages or versions are missing from the bundle (see above). No changes were made. Rebuild the bundle against current Debian $WANT_CODENAME updates."
    fi
    if grep -q '^Remv ' "$WORK/sim.log"; then
        grep '^Remv ' "$WORK/sim.log" >&2
        die "resolution would remove packages; refusing"
    fi
    N_NEW=$(grep -c '^Inst [^ ]* (' "$WORK/sim.log" || true)
    N_UPG=$(grep -c '^Inst [^ ]* \[' "$WORK/sim.log" || true)
    info "simulation OK: $N_NEW new package(s), $N_UPG upgrade(s), 0 removals"
    if [ "$N_UPG" -gt 0 ]; then
        info "packages that would be upgraded (required by bundled versions):"
        grep '^Inst [^ ]* \[' "$WORK/sim.log" | awk '{ print "    " $2 " " $3 " -> " $4 }' | tr -d '[]('
    fi
fi

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
    section "Preflight complete"
    info "all checks passed; no changes were made (--preflight-only)"
    exit 0
fi

# --------------------------------------------------------------------------
if [ "$SKIP_SYSTEM" -eq 0 ] && { [ "${N_NEW:-0}" -gt 0 ] || [ "${N_UPG:-0}" -gt 0 ]; }; then
    section "Install: system packages from the bundled repository"
    if [ "$ASSUME_YES" -ne 1 ]; then
        [ -r /dev/tty ] || die "no terminal for confirmation; rerun with --yes"
        printf 'Install/upgrade %s package(s) from the offline bundle? [y/N] ' "$((N_NEW + N_UPG))" >/dev/tty
        read -r answer </dev/tty || answer=""
        case "$answer" in y|Y|yes|YES) ;; *) die "aborted by user; no changes were made" ;; esac
    fi
    apt_local -q -y --no-install-recommends --no-remove \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
        install "${DEB_PKGS[@]}"
elif [ "$SKIP_SYSTEM" -eq 0 ]; then
    section "Install: system packages"
    info "all bundled system packages are already installed; nothing to do"
fi

section "Check: system packages"
missing=()
for p in "${DEB_PKGS[@]}"; do
    st=$(dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null || true)
    case "$st" in ii*) ;; *) missing+=("$p") ;; esac
done
if [ "${#missing[@]}" -gt 0 ]; then
    if [ "$SKIP_SYSTEM" -eq 1 ]; then
        die "--skip-system requested but these packages are not installed: ${missing[*]}. Run 'sudo ./$PROG' first."
    fi
    die "packages not installed after APT run: ${missing[*]}"
fi
[ -x "/usr/bin/python$PY_SHORT" ] || die "/usr/bin/python$PY_SHORT not found"
info "all ${#DEB_PKGS[@]} requested system packages are installed"

# --------------------------------------------------------------------------
section "Install: user Python environment (offline)"
FIND_LINKS=(--find-links "$BUNDLE_DIR/payload/wheels")
[ "$SDIST_COUNT" -eq 0 ] || FIND_LINKS+=(--find-links "$BUNDLE_DIR/payload/sdists")
if [ ! -e "$VENV" ]; then
    as_user mkdir -p -- "$(dirname -- "$VENV")"
    as_user "/usr/bin/python$PY_SHORT" -m venv -- "$VENV"
    info "created $VENV"
fi
[ -x "$VENV/bin/python" ] || die "environment python missing at $VENV/bin/python"
as_user "$VENV/bin/python" -m pip install --isolated --no-index --no-cache-dir --progress-bar off \
    --require-hashes "${FIND_LINKS[@]}" -r "$BUNDLE_DIR/requirements.lock"
as_user "$VENV/bin/python" -m pip check
LOCK_SHA=$(sha256sum -- "$BUNDLE_DIR/requirements.lock" | awk '{ print $1 }')
printf 'madd-python-bundle\nbundle=%s\nlock_sha256=%s\ninstalled_utc=%s\n' \
    "$BUNDLE_NAME" "$LOCK_SHA" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" |
    as_user tee -- "$VENV/.madd-python-bundle" >/dev/null
info "environment ready: $VENV"

# --------------------------------------------------------------------------
if [ "$NO_LAUNCHERS" -eq 0 ]; then
    section "Install: convenience launchers in $BIN_DIR"
    as_user mkdir -p -- "$BIN_DIR"
    write_launcher() {
        local body=$2 dest="$BIN_DIR/$1"
        if [ -e "$dest" ] && ! grep -q '^# madd-python-bundle launcher$' -- "$dest"; then
            warn "$dest exists and was not created by this bundle; leaving it unchanged"
            return 0
        fi
        printf '#!/bin/sh\n# madd-python-bundle launcher\n# Environment: %s\nVENV='"'"'%s'"'"'\n%s\n' \
            "$VENV" "$VENV" "$body" | as_user tee -- "$dest.tmp" >/dev/null
        as_user chmod 0755 -- "$dest.tmp"
        as_user mv -f -- "$dest.tmp" "$dest"
        info "wrote $dest"
    }
    # Launcher bodies are literal shell text, expanded when the launcher runs.
    # shellcheck disable=SC2016
    write_launcher madd-python 'exec "$VENV/bin/python" "$@"'
    # shellcheck disable=SC2016
    write_launcher madd-pip 'exec "$VENV/bin/python" -m pip "$@"'
    # shellcheck disable=SC2016
    write_launcher madd-python-env '# With arguments: run that command inside the environment.
# Without: start an interactive shell with the environment activated (exit to leave).
VIRTUAL_ENV="$VENV"; PATH="$VENV/bin:$PATH"; export VIRTUAL_ENV PATH; unset PYTHONHOME
if [ "$#" -gt 0 ]; then exec "$@"; fi
echo "madd-python environment active ($VENV). Type exit to leave."
exec "${SHELL:-/bin/sh}" -i'
fi

# --------------------------------------------------------------------------
section "Smoke tests"
SMOKE_FAIL=0
as_user /usr/bin/python3 --version || SMOKE_FAIL=1
if [ "$NO_LAUNCHERS" -eq 0 ] && [ -x "$BIN_DIR/madd-python" ]; then
    printf 'madd-python --version: '
    as_user "$BIN_DIR/madd-python" --version || SMOKE_FAIL=1
fi
as_user "$VENV/bin/python" -m pip --version || SMOKE_FAIL=1
info "system python3 standard-library checks:"
as_user /usr/bin/python3 -B "$BUNDLE_DIR/scripts/verify_python.py" || SMOKE_FAIL=1
info "environment checks:"
as_user "$VENV/bin/python" -B "$BUNDLE_DIR/scripts/smoke_test.py" || SMOKE_FAIL=1

section "Summary"
info "system packages: installed from offline bundle ($PROFILE_MATCH)"
info "environment:     $VENV"
info "activate with:   source $VENV/bin/activate"
if [ "$NO_LAUNCHERS" -eq 0 ]; then
    # Debian's default ~/.profile adds ~/.local/bin to PATH at login when it exists.
    if grep -qs '\.local/bin' -- "$TARGET_HOME/.profile" "$TARGET_HOME/.bash_profile" "$TARGET_HOME/.bashrc"; then
        info "launchers: $BIN_DIR (added to PATH by your shell startup files at next login)"
    else
        info "launchers are in $BIN_DIR, which your shell startup files do not add to PATH."
        info "Optional (not done automatically): add this line to ~/.bashrc:"
        # shellcheck disable=SC2016
        printf '    export PATH="%s:$PATH"\n' "$BIN_DIR"
    fi
fi
if [ "$SMOKE_FAIL" -ne 0 ]; then
    die "installation finished but smoke tests FAILED (see above)"
fi
info "smoke tests: PASS"
info "done."
