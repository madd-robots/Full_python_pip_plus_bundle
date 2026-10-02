#!/usr/bin/env bash
# verify.sh - read-only integrity and consistency check of this offline bundle.
# Makes no changes and no network connections. Safe to run as a normal user.
# Exit status: 0 = PASS, non-zero = FAIL (do not install).
set -Eeuo pipefail
umask 077
PATH=/usr/local/bin:/usr/bin:/bin
export PATH LC_ALL=C

BUNDLE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
QUIET=0
SUMS_FILE=""

usage() {
    printf 'Usage: %s [--quiet] [--sums FILE]\n' "$0"
    printf '  --sums FILE  verify against a trusted copy of SHA256SUMS (used by install.sh)\n'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --quiet) QUIET=1 ;;
        --sums) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; SUMS_FILE=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

FAILS=0
say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
pass() { say "  PASS  $*"; }
fail() { printf '  FAIL  %s\n' "$*" >&2; FAILS=$((FAILS + 1)); }
die() { printf 'verify.sh: FATAL: %s\n' "$*" >&2; exit 1; }

for cmd in sha256sum awk sed grep sort find comm cmp; do
    command -v "$cmd" >/dev/null 2>&1 || die "required command not found: $cmd"
done

cd -- "$BUNDLE_DIR"
[ -n "$SUMS_FILE" ] || SUMS_FILE="$BUNDLE_DIR/SHA256SUMS"
[ -f "$SUMS_FILE" ] || die "missing checksum file: $SUMS_FILE"

TMPD=$(mktemp -d "${TMPDIR:-/tmp}/madd-verify.XXXXXXXX")
cleanup() {
    # TMPD is created above by mktemp; only remove that exact directory.
    case "${TMPD:-}" in
        "${TMPDIR:-/tmp}"/madd-verify.*) [ -d "$TMPD" ] && rm -rf -- "$TMPD" ;;
    esac
}
trap cleanup EXIT

kv() { sed -n "s/^$1=//p" BUILD_INFO.txt | head -n 1; }

say "Verifying bundle: $BUNDLE_DIR"

# 1. Required files.
for f in README.md MANIFEST.txt BUILD_INFO.txt profile.conf requirements.in requirements.lock \
         deb-manifest.txt deb-install-list.txt wheel-manifest.txt wireless-audit.txt \
         install.sh verify.sh uninstall-user-env.sh scripts/smoke_test.py scripts/verify_python.py \
         payload/debs/Packages payload/debs/Packages.gz payload/debs/Release; do
    [ -f "$f" ] || fail "missing required file: $f"
done
[ "$FAILS" -eq 0 ] && pass "required files present"

# 2. Checksum list sanity: well-formed lines, safe relative paths only.
if awk '!/^[0-9a-f]{64}  [A-Za-z0-9._+~%\/-]+$/ { bad=1 } $2 ~ /(^\/|(^|\/)\.\.(\/|$))/ { bad=1 } END { exit bad }' "$SUMS_FILE"; then
    pass "SHA256SUMS format and paths"
else
    die "SHA256SUMS contains malformed lines or unsafe paths"
fi

# 3. Every listed file exists and matches (missing or mismatched = FAIL).
if sha256sum --check --strict --quiet -- "$SUMS_FILE" >"$TMPD/check.out" 2>&1; then
    pass "SHA-256 of $(wc -l <"$SUMS_FILE") files"
else
    sed -n '1,40p' "$TMPD/check.out" >&2
    fail "SHA-256 verification failed (see above)"
fi

# 4. No unexpected files, symlinks, or special files.
awk '{ print $2 }' "$SUMS_FILE" | sort >"$TMPD/listed"
find . -type f ! -path ./SHA256SUMS -printf '%P\n' | sort >"$TMPD/present"
extra=$(comm -13 "$TMPD/listed" "$TMPD/present")
if [ -n "$extra" ]; then
    printf '%s\n' "$extra" | sed -n '1,20p' >&2
    fail "unexpected files not covered by SHA256SUMS"
else
    pass "no unlisted files"
fi
special=$(find . ! -type f ! -type d -printf '%P\n')
if [ -n "$special" ]; then
    fail "symlinks or special files present: $special"
else
    pass "no symlinks or special files"
fi

# 5. Manifest cross-checks against SHA256SUMS.
DEB_ARCH=$(kv DEB_ARCH)
[ -n "$DEB_ARCH" ] || fail "BUILD_INFO.txt lacks DEB_ARCH"

awk 'NR==FNR { sum[$2]=$1; next } /^#/ { next }
     { p="payload/debs/" $4; if (!(p in sum) || sum[p] != $5) { print "deb-manifest mismatch: " $4; bad=1 } }
     END { exit bad }' "$SUMS_FILE" deb-manifest.txt || fail "deb-manifest.txt disagrees with SHA256SUMS"

awk -v a="$DEB_ARCH" '/^#/ { next } $3 != a && $3 != "all" { print "foreign-architecture deb: " $4; bad=1 } END { exit bad }' \
    deb-manifest.txt || fail "deb-manifest.txt contains packages for another architecture"

awk 'NR==FNR { sum[$2]=$1; next } /^#/ { next }
     { p="payload/" ($3 == "wheel" ? "wheels/" : "sdists/") $4
       if (!(p in sum) || sum[p] != $5) { print "wheel-manifest mismatch: " $4; bad=1 } }
     END { exit bad }' "$SUMS_FILE" wheel-manifest.txt || fail "wheel-manifest.txt disagrees with SHA256SUMS"

# Every lock hash must correspond to exactly one bundled distribution.
grep -o 'sha256:[0-9a-f]\{64\}' requirements.lock | sed 's/^sha256://' | sort >"$TMPD/lockhashes"
awk '/^#/ { next } { print $5 }' wheel-manifest.txt | sort >"$TMPD/manhashes"
if cmp -s "$TMPD/lockhashes" "$TMPD/manhashes"; then
    pass "requirements.lock matches wheel-manifest.txt ($(wc -l <"$TMPD/lockhashes") distributions)"
else
    fail "requirements.lock and wheel-manifest.txt disagree"
fi

# Payload directories contain exactly the manifest files.
awk '/^#/ { next } { print $4 }' deb-manifest.txt | sort >"$TMPD/debs.man"
find payload/debs -maxdepth 1 -type f -name '*.deb' -printf '%f\n' | sort >"$TMPD/debs.dir"
sed -n 's|^Filename: \./||p' payload/debs/Packages | sort >"$TMPD/debs.idx"
if cmp -s "$TMPD/debs.man" "$TMPD/debs.dir" && cmp -s "$TMPD/debs.man" "$TMPD/debs.idx"; then
    pass "local APT repository index matches $(wc -l <"$TMPD/debs.man") .deb files"
else
    fail "payload/debs, Packages index, and deb-manifest.txt disagree"
fi
awk '/^#/ { next } $3 == "wheel" { print $4 }' wheel-manifest.txt | sort >"$TMPD/whl.man"
find payload/wheels -maxdepth 1 -type f -printf '%f\n' | sort >"$TMPD/whl.dir"
cmp -s "$TMPD/whl.man" "$TMPD/whl.dir" || fail "payload/wheels does not match wheel-manifest.txt"
awk '/^#/ { next } $3 == "sdist" { print $4 }' wheel-manifest.txt | sort >"$TMPD/sd.man"
find payload/sdists -maxdepth 1 -type f -printf '%f\n' | sort >"$TMPD/sd.dir"
cmp -s "$TMPD/sd.man" "$TMPD/sd.dir" || fail "payload/sdists does not match wheel-manifest.txt"

# Package index SHA256 values must match the files (APT re-checks this too).
awk '/^Filename: / { f=$2; sub(/^\.\//, "", f) } /^SHA256: / { print $2 "  payload/debs/" f }' \
    payload/debs/Packages >"$TMPD/idx.sums"
if (sha256sum --check --strict --quiet -- "$TMPD/idx.sums") >/dev/null 2>&1; then
    pass "Packages index SHA256 fields match .deb files"
else
    fail "Packages index SHA256 fields do not match .deb files"
fi
for idx in Packages Packages.gz; do
    want=$(awk -v n="$idx" '$3 == n { print $1 }' payload/debs/Release)
    got=$(sha256sum -- "payload/debs/$idx" | awk '{ print $1 }')
    if [ -z "$want" ] || [ "$want" != "$got" ]; then fail "Release hash mismatch for $idx"; fi
done

# 6. Counts declared in BUILD_INFO.txt.
[ "$(kv DEB_COUNT)" = "$(wc -l <"$TMPD/debs.man" | tr -d ' ')" ] || fail "DEB_COUNT mismatch"
[ "$(kv WHEEL_COUNT)" = "$(wc -l <"$TMPD/whl.man" | tr -d ' ')" ] || fail "WHEEL_COUNT mismatch"
[ "$(kv SDIST_COUNT)" = "$(wc -l <"$TMPD/sd.man" | tr -d ' ')" ] || fail "SDIST_COUNT mismatch"

# 7. Wheel platform tags: nothing for another OS/architecture.
WHEEL_ARCH=$(kv UNAME_MACHINE)
bad_whl=$(awk -v arch="$WHEEL_ARCH" '/^#/ || $3 != "wheel" { next }
    { n=$4; sub(/\.whl$/, "", n); k=split(n, parts, "-"); plat=parts[k]
      m=split(plat, ps, ".")
      for (i = 1; i <= m; i++) if (ps[i] != "any" && ps[i] !~ ("^manylinux(1|2010|2014|_2_[0-9]+)_" arch "$")) { print $4; break } }' \
    wheel-manifest.txt)
if [ -n "$bad_whl" ]; then
    fail "wheels for another platform: $bad_whl"
else
    pass "all wheels are 'any' or manylinux $WHEEL_ARCH"
fi
grep -q '^RESULT: PASS$' wireless-audit.txt || fail "wireless-audit.txt does not report PASS"

say ""
say "Bundle:  $(kv BUNDLE_NAME)  built $(kv BUILD_DATE_UTC)"
say "Target:  $(kv TARGET_DISTRIBUTION) / Debian $(kv DEBIAN_VERSION) ($(kv DEBIAN_CODENAME)) $(kv DEB_ARCH)"
say "Python:  $(kv PYTHON_FULL_VERSION) ($(kv PYTHON_ABI)), Debian package $(kv PYTHON_DEB_VERSION)"
if command -v dpkg >/dev/null 2>&1; then
    say "This machine: dpkg architecture $(dpkg --print-architecture), uname -m $(uname -m)"
else
    say "This machine: uname -m $(uname -m) (no dpkg; install.sh will check the target)"
fi

if [ "$FAILS" -ne 0 ]; then
    printf 'VERIFY: FAIL (%d problem(s)). Do NOT install this bundle.\n' "$FAILS" >&2
    exit 1
fi
say "VERIFY: PASS"
