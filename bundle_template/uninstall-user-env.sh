#!/usr/bin/env bash
# uninstall-user-env.sh - remove ONLY the user-space pieces this bundle created:
#   * the Python environment (default ~/.venvs/madd-python), identified by its
#     .madd-python-bundle marker file
#   * the madd-python / madd-pip / madd-python-env launchers in ~/.local/bin,
#     identified by their "# madd-python-bundle launcher" marker line
# System (Debian) packages are never removed by this script; see the note it prints.
set -Eeuo pipefail
umask 022
PATH=/usr/local/bin:/usr/bin:/bin
export PATH LC_ALL=C

BUNDLE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ASSUME_YES=0
DRY_RUN=0
VENV=""

usage() {
    cat <<EOF
Usage: ./uninstall-user-env.sh [--venv DIR] [--dry-run] [--yes]
Run as the user who owns the environment (not with sudo).
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --venv) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; VENV=$2; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

die() { printf 'uninstall-user-env.sh: STOP: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
    die "run this as the user who owns the environment, not as root"
fi
HOME_DIR=$(getent passwd "$(id -un)" | cut -d: -f6)
if [ -z "$HOME_DIR" ] || [ ! -d "$HOME_DIR" ]; then die "cannot determine your home directory"; fi
[ -n "$VENV" ] || VENV="$HOME_DIR/.venvs/madd-python"
case "$VENV" in /*) ;; *) die "--venv must be an absolute path" ;; esac

targets=()
if [ -e "$VENV" ] || [ -L "$VENV" ]; then
    [ ! -L "$VENV" ] || die "$VENV is a symlink; refusing"
    [ -d "$VENV" ] || die "$VENV is not a directory; refusing"
    real=$(cd -- "$VENV" && pwd -P)
    case "$real" in
        "$HOME_DIR"/?*) ;;
        *) die "$real is outside your home directory; refusing" ;;
    esac
    [ "$real" != "$HOME_DIR" ] || die "refusing to remove your home directory"
    if [ ! -f "$real/.madd-python-bundle" ] || [ "$(head -n 1 "$real/.madd-python-bundle")" != madd-python-bundle ]; then
        die "$real has no madd-python-bundle marker; it was not created by this bundle. Not touching it."
    fi
    [ -f "$real/pyvenv.cfg" ] || die "$real does not look like a virtual environment; refusing"
    targets+=("$real")
else
    printf 'No environment at %s\n' "$VENV"
fi

launchers=()
for name in madd-python madd-pip madd-python-env; do
    f="$HOME_DIR/.local/bin/$name"
    if [ -f "$f" ] && [ ! -L "$f" ]; then
        if grep -q '^# madd-python-bundle launcher$' -- "$f"; then
            launchers+=("$f")
        else
            printf 'Leaving %s: not created by this bundle\n' "$f"
        fi
    fi
done

if [ "${#targets[@]}" -eq 0 ] && [ "${#launchers[@]}" -eq 0 ]; then
    printf 'Nothing of this bundle is installed for %s.\n' "$(id -un)"
else
    printf 'Will remove:\n'
    for t in "${targets[@]}"; do printf '  environment  %s\n' "$t"; done
    for t in "${launchers[@]}"; do printf '  launcher     %s\n' "$t"; done
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '(dry run: nothing removed)\n'
    else
        if [ "$ASSUME_YES" -ne 1 ]; then
            printf 'Proceed? [y/N] '
            read -r answer || answer=""
            case "$answer" in y|Y|yes|YES) ;; *) die "aborted; nothing removed" ;; esac
        fi
        for t in "${launchers[@]}"; do rm -f -- "$t"; done
        # Each target was validated above: real directory, inside $HOME, marker present.
        for t in "${targets[@]}"; do rm -rf -- "$t"; done
        printf 'User environment removed.\n'
    fi
fi

cat <<EOF

System packages were NOT removed. The Debian packages installed by install.sh
(python3, compilers, -dev headers, Tk, ...) are shared with other software and
MX tools. If you are sure you want to remove some of them, review the effect first:

    apt-get --simulate remove <package>

The top-level packages requested by this bundle are listed in:
    $BUNDLE_DIR/deb-install-list.txt
EOF
