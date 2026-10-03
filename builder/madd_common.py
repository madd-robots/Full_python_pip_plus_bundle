"""Shared helpers for the MADD offline bundle builder (Python stdlib only).

Used by madd_stage.py (orchestrator: Termux or Debian/Ubuntu host) and by
madd_debstage.py (Debian package stage: Debian/Ubuntu, e.g. the existing
ubuntu-claude-secure proot environment).
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILDER_DIR = REPO_ROOT / "builder"
TEMPLATE_DIR = REPO_ROOT / "bundle_template"
STAGING_MARKER = ".madd-python-staging"

PROFILE_REQUIRED = (
    "PROFILE_ID", "BUNDLE_NAME", "TARGET_DISTRIBUTION", "TARGET_FAMILY", "TARGET_OS_ID",
    "TARGET_VERSION_ID", "TARGET_CODENAME", "TARGET_KEYRING", "DEB_ARCH", "UNAME_MACHINE",
    "WHEEL_PLATFORM_ARCH", "REPO_IDS", "REPO_COMPONENTS", "STAGING_KEYRING",
    "STAGING_KEYRING_PACKAGE", "BASELINE_URI", "BASELINE_SUITES",
)


class BuildError(Exception):
    pass


def log(msg, tag="madd-stage"):
    print("[%s] %s" % (tag, msg), flush=True)


def run(cmd, *, cwd=None, env=None, capture=False, check=True, quiet=False, tag="madd-stage"):
    """Run a command given as an argument list (never through a shell)."""
    if not quiet:
        log("$ " + " ".join(str(c) for c in cmd), tag)
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=cwd,
        env=env,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )
    if check and proc.returncode != 0:
        if capture and proc.stdout:
            print(proc.stdout[-6000:], flush=True)
        raise BuildError("command failed (exit %d): %s" % (proc.returncode, cmd[0]))
    return proc


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_kv_file(path):
    """Parse KEY=VALUE lines without executing anything."""
    result = {}
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not m:
            raise BuildError("%s:%d: malformed line" % (path, lineno))
        result[m.group(1)] = m.group(2)
    return result


def read_list_file(path):
    items = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            items.append(line)
    return items


def load_profile(name):
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name or ""):
        raise BuildError("invalid profile name %r" % name)
    path = BUILDER_DIR / "profiles" / ("%s.conf" % name)
    if not path.is_file():
        raise BuildError("no such profile: %s" % path)
    prof = read_kv_file(path)
    for key in PROFILE_REQUIRED:
        if not prof.get(key):
            raise BuildError("profile %s missing %s" % (name, key))
    for rid in prof["REPO_IDS"].split():
        if not re.match(r"^[a-z0-9-]+$", rid):
            raise BuildError("invalid repository id %r" % rid)
        for key in ("REPO_%s_URI" % rid.upper(), "REPO_%s_SUITES" % rid.upper()):
            if not prof.get(key):
                raise BuildError("profile %s missing %s" % (name, key))
        if not prof["REPO_%s_URI" % rid.upper()].startswith("https://"):
            raise BuildError("repository %s must use https" % rid)
    if not prof["BASELINE_URI"].startswith("https://"):
        raise BuildError("baseline URI must use https")
    return prof


def parse_stanzas(text):
    """Yield (fields_dict, raw_stanza_text) from a Debian control-format file."""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip("\n")
        if not block.strip():
            continue
        fields = {}
        key = None
        for line in block.split("\n"):
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line
                continue
            k, sep, v = line.partition(":")
            if not sep:
                continue
            key = k.strip()
            fields[key] = v.strip()
        yield fields, block


def upstream_version(debver):
    v = debver.split(":", 1)[-1]
    if "-" in v:
        v = v.rsplit("-", 1)[0]
    return v


def read_os_release(path="/etc/os-release"):
    data = {}
    for line in Path(path).read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep:
            data[k] = v.strip().strip('"')
    return data


def safe_rmtree(path, expected_parent, required_prefix):
    """Delete a directory only if it is provably a staging dir created by us."""
    path = Path(path)
    if not str(path) or path.is_symlink():
        raise BuildError("refusing to delete suspicious path: %r" % str(path))
    resolved = path.resolve()
    parent = Path(expected_parent).resolve()
    if resolved.parent != parent:
        raise BuildError("refusing to delete %s: not directly under %s" % (resolved, parent))
    if not resolved.name.startswith(required_prefix):
        raise BuildError("refusing to delete %s: unexpected name" % resolved)
    if not (resolved / STAGING_MARKER).is_file():
        raise BuildError("refusing to delete %s: staging marker missing" % resolved)
    shutil.rmtree(resolved)


def safe_rmtree_child(path, parent):
    path = Path(path)
    if path.is_symlink() or path.resolve().parent != Path(parent).resolve():
        raise BuildError("refusing to delete %s" % path)
    shutil.rmtree(path)


def apt_lists_prefix(uri):
    """APT's lists/ file-name prefix for a repository URI (scheme stripped, '/' -> '_')."""
    rest = uri.split("://", 1)[1].rstrip("/")
    return rest.replace("/", "_")


def pin_preferences(records, origins):
    """APT preferences that allow exactly the bundled package versions.

    Every version from the bundled repositories' origins gets priority -1
    (never install) unless it is the exact bundled version of that package.
    Versions only in the local dpkg status (already installed) are unaffected.
    """
    out = []
    for origin in sorted(origins):
        if not re.match(r"^[A-Za-z0-9 ._-]+$", origin):
            raise BuildError("unexpected repository Origin %r" % origin)
        out.append("Explanation: madd: nothing from %s except the bundled versions\n"
                   "Package: *\nPin: release o=%s\nPin-Priority: -1\n" % (origin, origin))
    for r in sorted(records, key=lambda r: r["name"]):
        out.append("Package: %s\nPin: version %s\nPin-Priority: 500\n" % (r["name"], r["version"]))
    return "\n".join(out)
