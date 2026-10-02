#!/usr/bin/env python3
"""Staging-side builder for the MADD offline Python bundle.

Runs inside the Ubuntu staging environment (ubuntu-claude-secure under
proot-distro, or a local Debian/Ubuntu host with --staging local). It never
needs root: Debian packages are resolved and downloaded by an isolated APT
instance (private config, state, cache and an empty dpkg status) configured
for the explicit target architecture, and Python wheels are fetched with
explicit target platform/ABI tags.

Phase 0 (bootstrap) runs on the system python3 with the stdlib only and
re-executes this file inside a private, hash-pinned tools venv.
"""

import argparse
import datetime
import email.parser
import gzip
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILDER_DIR = REPO_ROOT / "builder"
TEMPLATE_DIR = REPO_ROOT / "bundle_template"
BUNDLE_FORMAT_VERSION = "1"
STAGING_MARKER = ".madd-python-staging"
TOOLS_STAMP = ".madd-tools-stamp"
PYPI_INDEX = "https://pypi.org/simple"

EXECUTABLE_FILES = {"install.sh", "verify.sh", "uninstall-user-env.sh"}


class BuildError(Exception):
    pass


def log(msg):
    print("[madd-stage] " + msg, flush=True)


def run(cmd, *, cwd=None, env=None, capture=False, check=True, quiet=False):
    """Run a command given as an argument list (never through a shell)."""
    if not quiet:
        log("$ " + " ".join(str(c) for c in cmd))
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


# ---------------------------------------------------------------------------
# Phase 0: bootstrap private tools venv (stdlib only)
# ---------------------------------------------------------------------------

def bootstrap_tools_venv(out_dir):
    req = BUILDER_DIR / "tools-requirements.txt"
    stamp_value = "%s py%d.%d" % (sha256_file(req), sys.version_info[0], sys.version_info[1])
    cache = out_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    venv_dir = cache / "tools-venv"
    vpy = venv_dir / "bin" / "python"
    stamp = venv_dir / TOOLS_STAMP
    if vpy.exists() and stamp.is_file() and stamp.read_text().strip() == stamp_value:
        return vpy
    if venv_dir.exists():
        # Only remove a tools venv that this builder created (stamp present).
        if venv_dir.is_symlink() or not stamp.is_file():
            raise BuildError("%s exists but was not created by this builder; remove it manually" % venv_dir)
        if venv_dir.resolve().parent != cache.resolve():
            raise BuildError("unexpected tools venv location")
        shutil.rmtree(venv_dir)
    log("creating private tools venv at %s" % venv_dir)
    run([sys.executable, "-m", "venv", venv_dir])
    env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")
    run([vpy, "-m", "pip", "install", "--isolated", "--no-cache-dir", "--require-hashes",
         "--only-binary=:all:", "--no-deps", "--index-url", PYPI_INDEX, "-r", req], env=env)
    stamp.write_text(stamp_value + "\n")
    return vpy


# ---------------------------------------------------------------------------
# Debian control-file helpers
# ---------------------------------------------------------------------------

def parse_stanzas(text):
    """Yield (fields_dict, raw_stanza_text) from a Debian Packages file."""
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


def replace_field(stanza, field, value):
    out = []
    replaced = False
    skipping = False
    for line in stanza.split("\n"):
        if skipping and line[:1] in (" ", "\t"):
            continue
        skipping = False
        if line.startswith(field + ":"):
            out.append("%s: %s" % (field, value))
            replaced = True
            skipping = True
            continue
        out.append(line)
    if not replaced:
        raise BuildError("field %s missing from stanza" % field)
    return "\n".join(out)


def upstream_version(debver):
    v = debver.split(":", 1)[-1]
    if "-" in v:
        v = v.rsplit("-", 1)[0]
    return v


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class Builder:
    def __init__(self, args):
        self.args = args
        self.out_dir = Path(args.output).resolve()
        self.profile = read_kv_file(BUILDER_DIR / "profiles" / ("%s.conf" % args.profile))
        for key in ("PROFILE_ID", "BUNDLE_NAME", "DEBIAN_CODENAME", "DEB_ARCH",
                    "WHEEL_PLATFORM_ARCH", "UNAME_MACHINE", "DEBIAN_MIRROR",
                    "DEBIAN_SECURITY_MIRROR", "DEBIAN_SUITES", "DEBIAN_COMPONENTS"):
            if not self.profile.get(key):
                raise BuildError("profile missing %s" % key)
        self.bundle_name = self.profile["BUNDLE_NAME"]
        self.deb_arch = self.profile["DEB_ARCH"]
        self.wheel_arch = self.profile["WHEEL_PLATFORM_ARCH"]
        self.cache = self.out_dir / "cache"
        self.dist = self.out_dir / "dist"
        self.results = []  # (test name, PASS/FAIL/SKIP, detail)
        self.build_epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())
        self.staging = None

    # -- bookkeeping -------------------------------------------------------
    def record(self, name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        self.results.append((name, status, detail))
        log("TEST %-40s %s %s" % (name, status, detail))
        if not ok:
            raise BuildError("test failed: %s %s" % (name, detail))

    def check_storage(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.out_dir).free
        need = int(float(self.args.min_free_gb) * (1 << 30))
        log("free space at %s: %.2f GiB (required %.2f GiB)" % (self.out_dir, free / (1 << 30), need / (1 << 30)))
        if free < need:
            raise BuildError("insufficient storage: %.2f GiB free, %.2f GiB required "
                             "(override with --min-free-gb only if you know the build fits)"
                             % (free / (1 << 30), need / (1 << 30)))

    def make_staging(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.staging = Path(tempfile.mkdtemp(prefix="staging.", dir=self.out_dir))
        (self.staging / STAGING_MARKER).write_text("created by madd_stage.py\n")
        self.bundle_dir = self.staging / self.bundle_name
        self.payload = self.bundle_dir / "payload"
        for sub in ("debs", "wheels", "sdists"):
            (self.payload / sub).mkdir(parents=True)
        (self.bundle_dir / "scripts").mkdir(parents=True)
        log("staging directory: %s" % self.staging)

    def cleanup_staging(self):
        if self.staging and self.staging.exists():
            safe_rmtree(self.staging, self.out_dir, "staging.")

    # -- Debian keyring --------------------------------------------------
    def debian_keyring(self):
        if self.args.debian_keyring:
            kr = Path(self.args.debian_keyring).resolve()
            if not kr.is_file():
                raise BuildError("keyring not found: %s" % kr)
            return kr
        system = Path("/usr/share/keyrings/debian-archive-keyring.gpg")
        if system.is_file():
            return system
        # Fetch Ubuntu's signed debian-archive-keyring package via the staging
        # system's own (signature-verified) APT configuration; extract without root.
        kdir = self.cache / "keyring"
        kdir.mkdir(parents=True, exist_ok=True)
        extracted = kdir / "root" / "usr/share/keyrings/debian-archive-keyring.gpg"
        if extracted.is_file():
            return extracted
        for old in kdir.glob("debian-archive-keyring_*.deb"):
            old.unlink()
        proc = run(["apt-get", "download", "debian-archive-keyring"], cwd=kdir, capture=True, check=False)
        debs = sorted(kdir.glob("debian-archive-keyring_*.deb"))
        if proc.returncode != 0 or len(debs) != 1:
            print(proc.stdout or "", flush=True)
            raise BuildError(
                "could not obtain debian-archive-keyring from the staging system's APT sources. "
                "Run 'apt-get update' in the staging environment (or rerun the builder with "
                "--install-staging-prereqs), or pass --debian-keyring PATH.")
        run(["dpkg-deb", "-x", debs[0], kdir / "root"])
        if not extracted.is_file():
            raise BuildError("debian-archive-keyring package did not contain the expected keyring")
        return extracted

    # -- isolated APT ----------------------------------------------------
    def write_apt_config(self, root, sources_text, lists_dir, archives_dir):
        root = Path(root)
        for sub in ("etc/apt.conf.d", "etc/sources.list.d", "etc/preferences.d",
                    "etc/trusted.gpg.d", "state", "log", "cache"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        Path(lists_dir, "partial").mkdir(parents=True, exist_ok=True)
        Path(archives_dir, "partial").mkdir(parents=True, exist_ok=True)
        (root / "state" / "status").write_text("")
        (root / "etc" / "sources.list.d" / "madd.sources").write_text(sources_text)
        conf = root / "apt.conf"
        conf.write_text("\n".join([
            'APT::Architecture "%s";' % self.deb_arch,
            'APT::Architectures { "%s"; };' % self.deb_arch,
            'APT::Install-Recommends "false";',
            'APT::Install-Suggests "false";',
            'APT::Get::List-Cleanup "true";',
            'Acquire::Languages "none";',
            'Acquire::GzipIndexes "false";',
            'Acquire::IndexTargets::deb::Contents-deb::DefaultEnabled "false";',
            'Dir::Etc "%s/etc/";' % root,
            'Dir::Etc::main "%s/etc/apt.conf.main";' % root,
            'Dir::Etc::parts "%s/etc/apt.conf.d/";' % root,
            'Dir::Etc::sourcelist "%s/etc/sources.list";' % root,
            'Dir::Etc::sourceparts "%s/etc/sources.list.d/";' % root,
            'Dir::Etc::preferences "%s/etc/preferences";' % root,
            'Dir::Etc::preferencesparts "%s/etc/preferences.d/";' % root,
            'Dir::Etc::trusted "%s/etc/trusted.gpg";' % root,
            'Dir::Etc::trustedparts "%s/etc/trusted.gpg.d/";' % root,
            'Dir::State "%s/state/";' % root,
            'Dir::State::status "%s/state/status";' % root,
            'Dir::State::lists "%s/";' % lists_dir,
            'Dir::Cache "%s/cache/";' % root,
            'Dir::Cache::archives "%s/";' % archives_dir,
            'Dir::Log "%s/log/";' % root,
            'DPkg::Lock::Timeout "0";',
            "",
        ]))
        (root / "etc" / "sources.list").write_text("")
        return conf

    def apt_env(self, conf, offline=False):
        env = dict(os.environ, APT_CONFIG=str(conf), LC_ALL="C", DEBIAN_FRONTEND="noninteractive")
        if offline:
            for k in list(env):
                if k.lower().endswith("_proxy"):
                    del env[k]
            # Unreachable proxy: any accidental network fetch fails loudly.
            env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
        return env

    def resolve_debs(self):
        p = self.profile
        keyring = self.debian_keyring()
        log("Debian archive keyring: %s" % keyring)
        components = p["DEBIAN_COMPONENTS"]
        entries = [
            "Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nArchitectures: %s\nSigned-By: %s\n"
            % (p["DEBIAN_MIRROR"], p["DEBIAN_SUITES"], components, self.deb_arch, keyring),
        ]
        if p.get("DEBIAN_SECURITY_SUITES"):
            entries.append(
                "Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nArchitectures: %s\nSigned-By: %s\n"
                % (p["DEBIAN_SECURITY_MIRROR"], p["DEBIAN_SECURITY_SUITES"], components, self.deb_arch, keyring))
        for e in entries:
            if not re.search(r"^URIs: https://", e, re.M):
                raise BuildError("Debian mirrors must use https")
        apt_root = self.staging / "apt"
        lists = self.cache / ("apt-lists-%s" % p["PROFILE_ID"])
        archives = self.cache / ("apt-archives-%s" % p["PROFILE_ID"])
        conf = self.write_apt_config(apt_root, "\n".join(entries), lists, archives)
        env = self.apt_env(conf)
        run(["apt-get", "update", "-q"], env=env)

        self.deb_top = read_list_file(BUILDER_DIR / "debian-packages.txt")
        sim = run(["apt-get", "-s", "-q", "install", "--no-install-recommends", *self.deb_top],
                  env=env, capture=True)
        wanted = self.parse_apt_sim(sim.stdout)
        log("dependency closure: %d packages" % len(wanted))
        run(["apt-get", "-q", "-y", "install", "--download-only", "--no-install-recommends", *self.deb_top],
            env=env)

        index = self.load_index(lists)
        self.deb_records = []
        seen_names = set()
        for name, ver, arch in sorted(wanted):
            if arch not in (self.deb_arch, "all"):
                raise BuildError("resolved %s %s for wrong architecture %s" % (name, ver, arch))
            if name in seen_names:
                raise BuildError("duplicate package in closure: %s" % name)
            seen_names.add(name)
            fields, stanza = index.get((name, ver, arch), (None, None))
            if fields is None:
                raise BuildError("no signed index entry for %s %s %s" % (name, ver, arch))
            src = archives / ("%s_%s_%s.deb" % (name, ver.replace(":", "%3a"), arch))
            if not src.is_file():
                raise BuildError("APT did not download %s" % src.name)
            digest = sha256_file(src)
            size = src.stat().st_size
            if digest != fields.get("SHA256") or str(size) != fields.get("Size"):
                raise BuildError("hash/size mismatch against signed index for %s" % src.name)
            base = os.path.basename(fields["Filename"])
            if not re.match(r"^[A-Za-z0-9.+~_-]+\.deb$", base):
                raise BuildError("unexpected deb filename %r" % base)
            dst = self.payload / "debs" / base
            if dst.exists():
                raise BuildError("deb filename collision: %s" % base)
            shutil.copy2(src, dst)
            self.deb_records.append({
                "name": name, "version": ver, "arch": arch, "file": base,
                "sha256": digest, "size": size,
                "installed_kb": int(fields.get("Installed-Size", "0") or 0),
                "fields": fields, "stanza": replace_field(stanza, "Filename", "./" + base),
            })
        self.record("deb: dependency closure downloaded and hash-verified", True,
                    "%d packages, all %s/all" % (len(self.deb_records), self.deb_arch))

        by_name = {r["name"]: r for r in self.deb_records}
        for top in self.deb_top:
            if top not in by_name:
                # Requested name is virtual/transitional resolved to another package.
                log("note: top-level %s resolved via another package" % top)
        py = by_name.get("python3")
        libc = by_name.get("libc6")
        if not py or not libc:
            raise BuildError("python3/libc6 missing from closure")
        pyver = upstream_version(py["version"])
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", pyver)
        if not m:
            raise BuildError("cannot parse python3 version %r" % py["version"])
        self.py_major, self.py_minor = int(m.group(1)), int(m.group(2))
        self.py_full = "%s.%s.%s" % m.groups()
        self.py_short = "%d.%d" % (self.py_major, self.py_minor)
        self.py_abi = "cp%d%d" % (self.py_major, self.py_minor)
        if "python%s" % self.py_short not in by_name:
            raise BuildError("python%s interpreter package missing from closure" % self.py_short)
        g = re.match(r"^(\d+)\.(\d+)", upstream_version(libc["version"]))
        self.glibc = (int(g.group(1)), int(g.group(2)))
        self.python_deb_version = by_name["python%s" % self.py_short]["version"]
        log("target Python: %s (%s), glibc %d.%d" % (self.py_full, self.py_abi, *self.glibc))

    def parse_apt_sim(self, text):
        wanted = set()
        for line in text.splitlines():
            if line.startswith("Remv "):
                raise BuildError("unexpected removal in empty-status simulation: " + line)
            m = re.match(r"^Inst (\S+) (?:\[\S+\] )?\((\S+) .*\[(\S+)\]\)", line)
            if m:
                wanted.add((m.group(1).split(":")[0], m.group(2), m.group(3)))
        if not wanted:
            raise BuildError("APT simulation produced no packages")
        return wanted

    def load_index(self, lists):
        index = {}
        files = sorted(Path(lists).glob("*_Packages"))
        if not files:
            raise BuildError("no Packages indexes found in %s" % lists)
        for f in files:
            for fields, stanza in parse_stanzas(f.read_text(encoding="utf-8", errors="replace")):
                key = (fields.get("Package"), fields.get("Version"), fields.get("Architecture"))
                index.setdefault(key, (fields, stanza))
        return index

    def write_local_repo(self):
        debs = self.payload / "debs"
        body = "\n\n".join(r["stanza"] for r in sorted(self.deb_records, key=lambda r: r["name"])) + "\n"
        (debs / "Packages").write_text(body, encoding="utf-8")
        with open(debs / "Packages.gz", "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=self.build_epoch) as gz:
                gz.write(body.encode("utf-8"))
        date = time.strftime("%a, %d %b %Y %H:%M:%S UTC", time.gmtime(self.build_epoch))
        lines = [
            "Origin: madd-python-bundle",
            "Label: madd-python-bundle",
            "Suite: madd-offline",
            "Codename: madd-offline",
            "Architectures: %s all" % self.deb_arch,
            "Date: %s" % date,
            "Description: Offline local repository for %s (no network sources)" % self.bundle_name,
            "SHA256:",
        ]
        for name in ("Packages", "Packages.gz"):
            f = debs / name
            lines.append(" %s %d %s" % (sha256_file(f), f.stat().st_size, name))
        (debs / "Release").write_text("\n".join(lines) + "\n")

    # -- Python wheelhouse ------------------------------------------------
    def wheel_platforms(self):
        major, minor = self.glibc
        if major != 2:
            raise BuildError("unexpected glibc major version")
        plats = ["manylinux_2_%d_%s" % (m, self.wheel_arch) for m in range(minor, 4, -1)]
        legacy = {"manylinux2014": 17, "manylinux2010": 12, "manylinux1": 5}
        for tag, need in legacy.items():
            if minor >= need:
                plats.append("%s_%s" % (tag, self.wheel_arch))
        return plats

    def target_marker_env(self):
        return {
            "implementation_name": "cpython",
            "implementation_version": self.py_full,
            "os_name": "posix",
            "platform_machine": self.profile["UNAME_MACHINE"],
            "platform_release": "",
            "platform_system": "Linux",
            "platform_version": "",
            "python_full_version": self.py_full,
            "platform_python_implementation": "CPython",
            "python_version": self.py_short,
            "sys_platform": "linux",
        }

    def pip_env(self):
        return dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1",
                    PIP_NO_INPUT="1")

    def pip_target_args(self):
        args = ["--python-version", self.py_short, "--implementation", "cp",
                "--abi", self.py_abi, "--abi", "abi3", "--abi", "none"]
        for plat in self.wheel_platforms():
            args += ["--platform", plat]
        return args

    def pip_download(self, reqs, dest, *, no_deps=False, binary=True):
        cmd = [sys.executable, "-m", "pip", "download", "--isolated", "--index-url", PYPI_INDEX,
               "--cache-dir", self.cache / "pip", "--dest", dest, "--progress-bar", "off"]
        if binary:
            cmd += ["--only-binary=:all:"] + self.pip_target_args()
        else:
            cmd += ["--no-binary=:all:"]
        if no_deps:
            cmd.append("--no-deps")
        run(cmd + list(reqs), env=self.pip_env())

    def resolve_wheels(self):
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        req_lines = read_list_file(BUILDER_DIR / "requirements.in")
        self.top_reqs = [Requirement(r) for r in req_lines]
        sdist_names = {canonicalize_name(n) for n in read_list_file(BUILDER_DIR / "sdist-fallback.txt")}
        wheels = self.payload / "wheels"
        sdists = self.payload / "sdists"

        wheel_reqs = [str(r) for r in self.top_reqs if canonicalize_name(r.name) not in sdist_names]
        extra_reqs = []
        for name in sorted(sdist_names):
            match = [r for r in self.top_reqs if canonicalize_name(r.name) == name]
            spec = str(match[0]) if match else name
            self.pip_download([spec], sdists, no_deps=True, binary=False)
        for sd in sorted(sdists.iterdir()):
            extra_reqs += self.sdist_requirements(sd)

        try:
            self.pip_download(wheel_reqs + extra_reqs, wheels)
        except BuildError:
            raise BuildError(
                "wheel resolution failed for the target (CPython %s, %s, %s). If a package has no "
                "compatible wheel, add it to builder/sdist-fallback.txt (see pip output above)."
                % (self.py_short, self.py_abi, self.wheel_arch))

        for attempt in range(10):
            missing = self.check_closure(prune=False)
            if not missing:
                break
            log("closure pass %d: fetching %d requirement(s) missed by host-side marker evaluation"
                % (attempt + 1, len(missing)))
            self.pip_download(missing, wheels, no_deps=True)
        else:
            raise BuildError("wheel closure did not converge")
        self.check_closure(prune=True)
        self.check_wheel_tags()

    def sdist_requirements(self, sdist):
        """Static build + runtime requirements of a fallback sdist (no code execution)."""
        if sys.version_info < (3, 11):
            raise BuildError("sdist fallback requires staging Python >= 3.11 (tomllib)")
        import tomllib
        meta = self.read_dist_metadata(sdist)
        pyproject = self.read_member(sdist, "pyproject.toml")
        build_reqs = ["setuptools>=40.8.0", "wheel"]
        if pyproject is not None:
            data = tomllib.loads(pyproject.decode("utf-8"))
            build_reqs = list(data.get("build-system", {}).get("requires", build_reqs))
        dynamic = {d.lower() for d in meta.get_all("Dynamic") or []}
        if meta.get("Metadata-Version", "1.0") < "2.2" or "requires-dist" in dynamic:
            runtime = meta.get_all("Requires-Dist") or []
            if not runtime and pyproject is None:
                log("WARNING: %s has no static dependency metadata; assuming none. Verify manually."
                    % sdist.name)
        else:
            runtime = meta.get_all("Requires-Dist") or []
        log("sdist %s: build requires %s" % (sdist.name, build_reqs))
        return build_reqs + list(runtime)

    def read_member(self, path, member_name):
        if path.name.endswith(".whl"):
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if n.count("/") == 1 and n.endswith(".dist-info/" + member_name)]
                return zf.read(names[0]) if names else None
        if path.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(path, "r:gz") as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.count("/") == 1 and m.name.endswith("/" + member_name):
                        return tf.extractfile(m).read()
            return None
        if path.name.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if n.count("/") == 1 and n.endswith("/" + member_name)]
                return zf.read(names[0]) if names else None
        raise BuildError("unsupported distribution file %s" % path.name)

    def read_dist_metadata(self, path):
        raw = self.read_member(path, "METADATA" if path.name.endswith(".whl") else "PKG-INFO")
        if raw is None:
            raise BuildError("no metadata in %s" % path.name)
        return email.parser.BytesParser().parsebytes(raw, headersonly=True)

    def dist_files(self):
        from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
        dists = {}
        for sub in ("wheels", "sdists"):
            for f in sorted((self.payload / sub).iterdir()):
                if f.name.endswith(".whl"):
                    name, version, _b, _t = parse_wheel_filename(f.name)
                else:
                    name, version = parse_sdist_filename(f.name)
                name = canonicalize_name(name)
                if name in dists:
                    raise BuildError("multiple distributions for %s: %s, %s" % (name, dists[name]["file"].name, f.name))
                dists[name] = {"file": f, "version": version, "kind": sub}
        return dists

    def check_closure(self, prune):
        """Walk Requires-Dist with markers evaluated for the TARGET environment."""
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        env = self.target_marker_env()
        dists = self.dist_files()
        meta_cache = {}
        missing = []
        reachable = set()
        queue = [(canonicalize_name(r.name), frozenset(r.extras), r) for r in self.top_reqs]
        for sd in (d for d in dists.values() if d["kind"] == "sdists"):
            for req in self.sdist_requirements(sd["file"]):
                r = Requirement(req)
                queue.append((canonicalize_name(r.name), frozenset(r.extras), r))
        done = set()
        while queue:
            name, extras, req = queue.pop()
            d = dists.get(name)
            if d is None:
                missing.append(str(req))
                continue
            if not req.specifier.contains(d["version"], prereleases=True):
                raise BuildError("version conflict: %s required, %s bundled" % (req, d["version"]))
            reachable.add(name)
            if (name, extras) in done:
                continue
            done.add((name, extras))
            if name not in meta_cache:
                meta_cache[name] = self.read_dist_metadata(d["file"])
            for dep in meta_cache[name].get_all("Requires-Dist") or []:
                r = Requirement(dep)
                if r.marker is not None:
                    if not any(r.marker.evaluate(dict(env, extra=e)) for e in sorted(extras | {""})):
                        continue
                queue.append((canonicalize_name(r.name), frozenset(r.extras), r))
        if prune and not missing:
            for name, d in dists.items():
                if name not in reachable:
                    log("pruning %s: not required on the target (host-side marker artefact)" % d["file"].name)
                    d["file"].unlink()
        return sorted(set(missing))

    def check_wheel_tags(self):
        from packaging.tags import compatible_tags, cpython_tags
        from packaging.utils import parse_wheel_filename

        plats = self.wheel_platforms()
        ver = (self.py_major, self.py_minor)
        supported = set(cpython_tags(ver, abis=[self.py_abi, "abi3", "none"], platforms=plats))
        supported |= set(compatible_tags(ver, self.py_abi, plats))
        bad = []
        for f in sorted((self.payload / "wheels").glob("*.whl")):
            _n, _v, _b, tags = parse_wheel_filename(f.name)
            if not (set(tags) & supported):
                bad.append(f.name)
        others = [f.name for f in (self.payload / "wheels").iterdir() if not f.name.endswith(".whl")]
        self.record("wheel: all wheels match target tags", not bad and not others,
                    "incompatible: %s" % (bad + others) if bad or others else
                    "CPython %s %s linux %s" % (self.py_short, self.py_abi, self.wheel_arch))

    # -- manifests ---------------------------------------------------------
    def write_manifests(self):
        from packaging.utils import parse_wheel_filename

        b = self.bundle_dir
        shutil.copy2(BUILDER_DIR / "requirements.in", b / "requirements.in")
        dists = self.dist_files()
        self.wheel_records = []
        lock = [
            "# requirements.lock - frozen by build_madd_python_bundle.sh; do not edit.",
            "# Target: CPython %s (%s), linux %s, glibc %d.%d" % (self.py_full, self.py_abi, self.wheel_arch, *self.glibc),
            "# Install offline with: pip install --no-index --find-links payload/wheels "
            "--find-links payload/sdists --require-hashes -r requirements.lock",
        ]
        wm = ["# name version kind filename sha256 size tags"]
        for name in sorted(dists):
            d = dists[name]
            meta = self.read_dist_metadata(d["file"])
            digest = sha256_file(d["file"])
            tags = "sdist"
            if d["kind"] == "wheels":
                tags = ".".join(sorted(str(t) for t in parse_wheel_filename(d["file"].name)[3]))
            rec = {"name": meta["Name"], "canonical": name, "version": str(d["version"]), "kind": d["kind"],
                   "file": d["file"].name, "sha256": digest, "size": d["file"].stat().st_size,
                   "meta": meta}
            self.wheel_records.append(rec)
            lock.append("%s==%s \\\n    --hash=sha256:%s" % (rec["name"], rec["version"], digest))
            wm.append("%s %s %s %s %s %d %s" % (rec["name"], rec["version"], d["kind"][:-1], rec["file"],
                                                 digest, rec["size"], tags))
        (b / "requirements.lock").write_text("\n".join(lock) + "\n")
        (b / "wheel-manifest.txt").write_text("\n".join(wm) + "\n")

        dm = ["# package version architecture filename sha256 size installed-size-kb"]
        for r in sorted(self.deb_records, key=lambda r: r["name"]):
            dm.append("%s %s %s %s %s %d %d" % (r["name"], r["version"], r["arch"], r["file"],
                                                 r["sha256"], r["size"], r["installed_kb"]))
        (b / "deb-manifest.txt").write_text("\n".join(dm) + "\n")

        self.sdist_records = [r for r in self.wheel_records if r["kind"] == "sdists"]
        info = [
            ("BUNDLE_NAME", self.bundle_name),
            ("BUNDLE_FORMAT_VERSION", BUNDLE_FORMAT_VERSION),
            ("PROFILE_ID", self.profile["PROFILE_ID"]),
            ("TARGET_DISTRIBUTION", self.profile.get("TARGET_DISTRIBUTION", "")),
            ("DEBIAN_VERSION", self.profile.get("DEBIAN_VERSION", "")),
            ("DEBIAN_CODENAME", self.profile["DEBIAN_CODENAME"]),
            ("DEB_ARCH", self.deb_arch),
            ("UNAME_MACHINE", self.profile["UNAME_MACHINE"]),
            ("PYTHON_VERSION", self.py_short),
            ("PYTHON_FULL_VERSION", self.py_full),
            ("PYTHON_DEB_VERSION", self.python_deb_version),
            ("PYTHON_ABI", self.py_abi),
            ("GLIBC_VERSION", "%d.%d" % self.glibc),
            ("WHEEL_PLATFORM_MAX", self.wheel_platforms()[0]),
            ("DEB_TOP_LEVEL", " ".join(self.deb_top)),
            ("DEB_COUNT", str(len(self.deb_records))),
            ("DEB_TOTAL_BYTES", str(sum(r["size"] for r in self.deb_records))),
            ("DEB_INSTALLED_SIZE_KB", str(sum(r["installed_kb"] for r in self.deb_records))),
            ("WHEEL_COUNT", str(sum(1 for r in self.wheel_records if r["kind"] == "wheels"))),
            ("SDIST_COUNT", str(len(self.sdist_records))),
            ("PYDIST_TOTAL_BYTES", str(sum(r["size"] for r in self.wheel_records))),
            ("DEBIAN_SOURCES", "%s [%s] + %s [%s] (%s)" % (
                self.profile["DEBIAN_MIRROR"], self.profile["DEBIAN_SUITES"],
                self.profile["DEBIAN_SECURITY_MIRROR"], self.profile.get("DEBIAN_SECURITY_SUITES", ""),
                self.profile["DEBIAN_COMPONENTS"])),
            ("PYTHON_INDEX", PYPI_INDEX),
            ("BUILD_DATE_UTC", datetime.datetime.fromtimestamp(self.build_epoch, datetime.timezone.utc)
             .strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("BUILD_EPOCH", str(self.build_epoch)),
            ("BUILDER_HOST_ARCH", platform.machine()),
            ("BUILDER_STAGING", self.staging_os()),
        ]
        for k, v in info:
            if "\n" in v:
                raise BuildError("newline in BUILD_INFO value %s" % k)
        (b / "BUILD_INFO.txt").write_text("".join("%s=%s\n" % kv for kv in info))

    def staging_os(self):
        try:
            osr = read_os_release()
            return "%s (%s)" % (osr.get("PRETTY_NAME", "unknown"), os.environ.get("MADD_STAGING_LABEL", "local"))
        except OSError:
            return "unknown"

    def copy_templates(self):
        for src in sorted(TEMPLATE_DIR.rglob("*")):
            if src.is_dir() or "__pycache__" in src.parts:
                continue
            rel = src.relative_to(TEMPLATE_DIR)
            dst = self.bundle_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        shutil.copy2(BUILDER_DIR / "profiles" / ("%s.conf" % self.args.profile), self.bundle_dir / "profile.conf")
        top = self.bundle_dir / "deb-install-list.txt"
        top.write_text("".join(p + "\n" for p in self.deb_top))

    # -- wireless / radio exclusion audit -----------------------------------
    DEB_DENY = re.compile(
        r"^(bluez.*|blueman|bluetooth|libbluetooth.*|python3?-bluez|python3?-bleak|"
        r"gnome-bluetooth.*|bluedevil|libkf\d*bluezqt.*|wpasupplicant|wpagui|hostapd|"
        r"wireless-tools|wireless-regdb|iw|iwd|rfkill|crda|libiw\d*|libiw-dev|wavemon|"
        r"network-manager.*|libnm\d*|libnm-.*|gir1\.2-nm.*|modemmanager.*|libmm-glib.*|"
        r"libqmi.*|libmbim.*|libqrtr.*|ofono.*|connman.*|wicd.*|ppp|usb-modeswitch.*|"
        r"firmware-.*|.*-firmware|linux-image-.*|linux-modules-.*|libnfc.*|neard)$")
    PY_DENY = re.compile(
        r"^(pybluez|bluez|bleak|bleak-.*|bluepy|pygatt|bless|simplepyble|pybluez2|"
        r"bluetooth.*|pywifi|wifi|wireless|pywlan|python-networkmanager|networkmanager|"
        r"nmcli|sdbus-networkmanager|dbus-fast|pyobjc-framework-corebluetooth|winrt-.*bluetooth.*|"
        r"pyserial-bluetooth|pyusb-bluetooth)$")
    KEYWORDS = re.compile(
        r"(?i)\b(bluetooth|bluez|wi-?fi|wlan|802\.11|wireless|wpa_?supplicant|cellular|"
        r"modem|gsm|lte|umts|3gpp|rfkill|nl80211|cfg80211|zigbee|nfc)\b")
    PATH_DENY = re.compile(
        r"(?i)^\./(?:usr/)?lib/(?:firmware|modules)/|bluetooth|bluez|wpa_supplicant|"
        r"networkmanager|modemmanager|rfkill|nl80211|cfg80211|wireless|wlan|wwan|802154|"
        r"/(?:ofono|connman|iwd|hostapd)(?:/|$)")

    def wireless_audit(self):
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        # Allowlist lines: KIND NAME REGEX  # reason   (REGEX is matched against the finding)
        allow = []
        allow_file = BUILDER_DIR / "wireless-allowlist.txt"
        if allow_file.is_file():
            for raw in allow_file.read_text(encoding="utf-8").splitlines():
                line, _sep, reason = raw.partition("#")
                parts = line.split()
                if not parts:
                    continue
                if len(parts) != 3 or not reason.strip():
                    raise BuildError("malformed wireless-allowlist line (need KIND NAME REGEX # reason): %r" % raw)
                allow.append((parts[0], parts[1], re.compile(parts[2]), reason.strip()))
        report = ["Wireless / Bluetooth / cellular exclusion audit for %s" % self.bundle_name, ""]
        problems = []
        allowed = []

        def hit(kind, name, what):
            for akind, aname, rx, reason in allow:
                if akind == kind and aname == name and rx.search(what):
                    allowed.append("%s %s: %s  [allowlisted: %s]" % (kind, name, what, reason))
                    return
            problems.append("%s %s: %s" % (kind, name, what))

        report.append("Debian packages checked: %d" % len(self.deb_records))
        report.append("  - package names vs. deny pattern (bluez, wpasupplicant, iw, network-manager, "
                      "modemmanager, libqmi/libmbim, ofono, firmware-*, kernel modules, ...)")
        report.append("  - Depends/Pre-Depends/Recommends/Suggests/Provides/Breaks names vs. deny pattern")
        report.append("  - Section/Description/Tag fields vs. radio keyword pattern")
        report.append("  - every file path inside every .deb vs. firmware/kernel-module/bluetooth/wpa/rfkill/nl80211/wireless/wwan/NetworkManager/ModemManager paths")
        for r in self.deb_records:
            f = r["fields"]
            if self.DEB_DENY.match(r["name"]):
                hit("deb", r["name"], "name matches deny pattern")
            for fld in ("Depends", "Pre-Depends", "Recommends", "Suggests", "Provides", "Breaks", "Enhances"):
                for alt in re.split(r"[,|]", f.get(fld, "")):
                    dep = alt.strip().split(" ")[0].split(":")[0]
                    if dep and self.DEB_DENY.match(dep):
                        hit("deb", r["name"], "%s references %s" % (fld, dep))
            for fld in ("Section", "Description", "Tag"):
                m = self.KEYWORDS.search(f.get(fld, ""))
                if m:
                    hit("deb", r["name"], "%s keyword '%s'" % (fld, m.group(0)))
            for path in self.deb_paths(self.payload / "debs" / r["file"]):
                if self.PATH_DENY.search(path):
                    hit("deb", r["name"], "path %s" % path)
        report.append("")
        report.append("Python distributions checked: %d" % len(self.wheel_records))
        report.append("  - project names and all Requires-Dist names (including optional extras) vs. deny pattern")
        report.append("  - Summary/Keywords/Classifiers vs. radio keyword pattern")
        report.append("  - top-level importable package names inside each wheel")
        for w in self.wheel_records:
            meta = w["meta"]
            if self.PY_DENY.match(w["canonical"]):
                hit("py", w["canonical"], "name matches deny pattern")
            for dep in meta.get_all("Requires-Dist") or []:
                try:
                    dn = canonicalize_name(Requirement(dep).name)
                except Exception:
                    continue
                if self.PY_DENY.match(dn):
                    hit("py", w["canonical"], "Requires-Dist %s" % dn)
            text = " ".join([meta.get("Summary", "") or "", meta.get("Keywords", "") or ""] +
                            (meta.get_all("Classifier") or []))
            m = self.KEYWORDS.search(text)
            if m:
                hit("py", w["canonical"], "metadata keyword '%s'" % m.group(0))
            if w["file"].endswith(".whl"):
                with zipfile.ZipFile(self.payload / "wheels" / w["file"]) as zf:
                    tops = {n.split("/")[0].split(".")[0].lower() for n in zf.namelist()}
                for t in tops:
                    if self.PY_DENY.match(canonicalize_name(t)):
                        hit("py", w["canonical"], "top-level module %s" % t)
        report.append("")
        report.append("Reviewed exceptions (%d):" % len(allowed))
        report += ["  " + a for a in allowed] or ["  none"]
        report.append("")
        if problems:
            for prob in problems:
                log("AUDIT: " + prob)
            report.append("RESULT: FAIL")
            report += ["  " + p for p in problems]
        else:
            report.append("RESULT: PASS")
            report.append("No intentionally bundled package or resolved dependency in this build was identified "
                          "as a Bluetooth, Wi-Fi, cellular, or wireless-radio package according to the "
                          "dependency, metadata, file-path and manifest checks listed above.")
            report.append("Note: CPython's socket module may define AF_BLUETOOTH constants on Debian; "
                          "this is a stdlib socket-family constant, not a bundled Bluetooth package.")
        (self.bundle_dir / "wireless-audit.txt").write_text("\n".join(report) + "\n")
        self.record("audit: wireless/bluetooth/cellular exclusion", not problems,
                    "; ".join(problems[:5]) if problems else "%d debs, %d python dists"
                    % (len(self.deb_records), len(self.wheel_records)))

    def deb_paths(self, deb):
        proc = run(["dpkg-deb", "--contents", deb], capture=True, quiet=True)
        paths = []
        for line in proc.stdout.splitlines():
            parts = line.split(None, 5)
            if len(parts) == 6:
                paths.append(parts[5].split(" -> ")[0])
        return paths

    # -- static checks ---------------------------------------------------
    def static_checks(self):
        shells = sorted(p for p in self.bundle_dir.rglob("*.sh"))
        problems = []
        for sh in shells:
            if run(["bash", "-n", sh], check=False).returncode != 0:
                problems.append("bash -n %s" % sh.name)
            if "`" in sh.read_text(encoding="utf-8"):
                problems.append("backtick in %s" % sh.name)
        self.record("static: bash -n and zero backticks", not problems,
                    "; ".join(problems) or "%d shell scripts" % len(shells))
        if shutil.which("shellcheck"):
            proc = run(["shellcheck", "-x", "-S", "warning", *shells], check=False, capture=True)
            if proc.stdout:
                print(proc.stdout, flush=True)
            self.record("static: shellcheck (warning level)", proc.returncode == 0, "")
        else:
            self.results.append(("static: shellcheck", "SKIP", "shellcheck not installed in staging"))
        pys = sorted(self.bundle_dir.rglob("*.py"))
        bad = []
        for py in pys:
            try:
                compile(py.read_text(encoding="utf-8"), str(py), "exec")
            except SyntaxError as exc:
                bad.append("%s: %s" % (py.name, exc))
            if "shell=True" in py.read_text(encoding="utf-8"):
                bad.append("shell=True in %s" % py.name)
        self.record("static: python syntax, no shell=True", not bad, "; ".join(bad) or "%d files" % len(pys))

    # -- offline resolution tests ---------------------------------------
    def offline_apt_test(self):
        apt_root = self.staging / "apt-offline-test"
        repo = self.payload / "debs"
        sources = ("Types: deb\nURIs: file:%s\nSuites: ./\nTrusted: yes\n" % repo)
        if " " in str(repo):
            raise BuildError("staging path must not contain spaces")
        conf = self.write_apt_config(apt_root, sources, apt_root / "lists", apt_root / "archives")
        env = self.apt_env(conf, offline=True)
        run(["apt-get", "update", "-q"], env=env)
        sim = run(["apt-get", "-s", "-q", "install", "--no-install-recommends", *self.deb_top],
                  env=env, capture=True, check=False)
        ok = sim.returncode == 0
        got = self.parse_apt_sim(sim.stdout) if ok else set()
        expect = {(r["name"], r["version"], r["arch"]) for r in self.deb_records}
        self.record("deb: offline resolution from local repo only", ok and got == expect,
                    "%d/%d packages resolvable from empty system" % (len(got), len(expect)))

    def offline_pip_test(self):
        dest = self.staging / "pip-offline-test"
        dest.mkdir()
        env = self.pip_env()
        for k in list(env):
            if k.lower().endswith("_proxy"):
                del env[k]
        env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
        cmd = [sys.executable, "-m", "pip", "download", "--isolated", "--no-index", "--no-cache-dir",
               "--find-links", self.payload / "wheels", "--find-links", self.payload / "sdists",
               "--require-hashes", "--dest", dest, "--progress-bar", "off",
               "-r", self.bundle_dir / "requirements.lock"]
        if self.sdist_records:
            cmd += ["--no-deps"]
        else:
            cmd += ["--only-binary=:all:"] + self.pip_target_args()
        proc = run(cmd, env=env, check=False, capture=True)
        n = len(list(dest.iterdir()))
        self.record("wheel: offline hash-checked resolution of lock", proc.returncode == 0 and n == len(self.wheel_records),
                    "%d/%d distributions" % (n, len(self.wheel_records)) if proc.returncode == 0 else proc.stdout[-500:])

    # -- integrity, archive, extraction tests -----------------------------
    def write_sums(self):
        b = self.bundle_dir
        files = sorted(p for p in b.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
        manifest = ["# %s - file inventory (path, bytes)" % self.bundle_name]
        for f in files:
            rel = f.relative_to(b).as_posix()
            if not re.match(r"^[A-Za-z0-9._+~%/-]+$", rel):
                raise BuildError("unsafe filename in bundle: %r" % rel)
        mpath = b / "MANIFEST.txt"
        for f in files + ([mpath] if mpath not in files else []):
            if f == mpath:
                continue
            manifest.append("%s %d" % (f.relative_to(b).as_posix(), f.stat().st_size))
        manifest.append("MANIFEST.txt -")
        manifest.append("SHA256SUMS -")
        mpath.write_text("\n".join(manifest) + "\n")
        files = sorted(p for p in b.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
        lines = ["%s  %s" % (sha256_file(f), f.relative_to(b).as_posix()) for f in files]
        (b / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        self.record("integrity: SHA256SUMS generated", True, "%d files" % len(lines))

    def make_tarball(self):
        tar_path = self.staging / ("%s.tar.gz" % self.bundle_name)

        def norm(ti):
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = self.build_epoch
            if ti.isdir():
                ti.mode = 0o755
            elif ti.isfile():
                rel = ti.name.split("/", 1)[-1]
                ti.mode = 0o755 if rel in EXECUTABLE_FILES else 0o644
            else:
                raise BuildError("refusing non-regular member %s" % ti.name)
            return ti

        with open(tar_path, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=self.build_epoch, compresslevel=6) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
                    paths = [self.bundle_dir] + sorted(self.bundle_dir.rglob("*"))
                    for p in paths:
                        arc = (Path(self.bundle_name) / p.relative_to(self.bundle_dir)).as_posix() \
                            if p != self.bundle_dir else self.bundle_name
                        tf.add(p, arcname=arc, recursive=False, filter=norm)
        self.tar_path = tar_path
        self.record("archive: tar.gz created", tar_path.stat().st_size > 0,
                    "%.1f MiB" % (tar_path.stat().st_size / (1 << 20)))

    def extraction_tests(self):
        ext = self.staging / "extract-test"
        ext.mkdir()
        proc = run(["tar", "-xzf", self.tar_path, "-C", ext], check=False)
        root = ext / self.bundle_name
        self.record("archive: clean extraction with tar", proc.returncode == 0 and root.is_dir(), "")
        vproc = run(["./verify.sh"], cwd=root, check=False, capture=True)
        print(vproc.stdout[-1500:], flush=True)
        self.record("verify.sh: pass on pristine extraction", vproc.returncode == 0, "")
        # Negative tests on the throwaway extraction.
        with open(root / "README.md", "ab") as fh:
            fh.write(b"tampered\n")
        tproc = run(["./verify.sh"], cwd=root, check=False, capture=True)
        self.record("verify.sh: rejects modified file", tproc.returncode != 0, "exit %d" % tproc.returncode)
        victim = sorted((root / "payload" / "wheels").glob("*.whl"))[0]
        victim.unlink()
        mproc = run(["./verify.sh"], cwd=root, check=False, capture=True)
        self.record("verify.sh: rejects missing file", mproc.returncode != 0, "exit %d" % mproc.returncode)
        safe_rmtree_child(ext, self.staging)

    def finalize(self):
        digest = sha256_file(self.tar_path)
        name = self.tar_path.name
        sha_tmp = self.staging / (name + ".sha256")
        sha_tmp.write_text("%s  %s\n" % (digest, name))
        report = self.staging / ("%s.build-report.txt" % self.bundle_name)
        lines = ["Build report for %s" % name, "sha256 %s" % digest,
                 "size_bytes %d" % self.tar_path.stat().st_size, ""]
        lines += ["%-50s %s %s" % r for r in self.results]
        lines += ["", "BUILD_INFO:"] + (self.bundle_dir / "BUILD_INFO.txt").read_text().splitlines()
        report.write_text("\n".join(lines) + "\n")
        self.dist.mkdir(parents=True, exist_ok=True)
        # Atomic replacement: the previous bundle stays until the new one is validated.
        for f in (self.tar_path, sha_tmp, report, self.bundle_dir / "wireless-audit.txt"):
            target = self.dist / (f.name if f.name != "wireless-audit.txt" else
                                  "%s.wireless-audit.txt" % self.bundle_name)
            tmp = self.dist / (".incoming." + target.name)
            if f.parent == self.staging:
                os.replace(f, tmp)
            else:
                shutil.copy2(f, tmp)
            os.replace(tmp, target)
        log("FINAL ARCHIVE: %s" % (self.dist / name))
        log("SHA256: %s" % digest)
        for r in self.results:
            log("%-50s %s %s" % r)

    def build(self):
        self.check_storage()
        self.make_staging()
        ok = False
        try:
            self.resolve_debs()
            self.write_local_repo()
            self.resolve_wheels()
            self.copy_templates()
            self.write_manifests()
            self.wireless_audit()
            self.static_checks()
            self.offline_apt_test()
            self.offline_pip_test()
            self.write_sums()
            self.make_tarball()
            self.extraction_tests()
            self.finalize()
            ok = True
        finally:
            if ok or not self.args.keep_staging:
                self.cleanup_staging()
            else:
                log("staging kept for inspection: %s" % self.staging)


def safe_rmtree_child(path, parent):
    path = Path(path)
    if path.is_symlink() or path.resolve().parent != Path(parent).resolve():
        raise BuildError("refusing to delete %s" % path)
    shutil.rmtree(path)


def read_os_release():
    data = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep:
            data[k] = v.strip().strip('"')
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output", required=True, help="output directory (cache/, dist/, staging.*)")
    ap.add_argument("--profile", default="mx25-amd64")
    ap.add_argument("--min-free-gb", default="4")
    ap.add_argument("--debian-keyring", default="")
    ap.add_argument("--keep-staging", action="store_true")
    ap.add_argument("--check-storage-only", action="store_true")
    ap.add_argument("--in-tools-venv", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", args.profile):
        raise BuildError("invalid profile name")
    out_dir = Path(args.output).resolve()
    try:
        builder = Builder(args)
        if args.check_storage_only:
            builder.check_storage()
            return 0
        if not args.in_tools_venv:
            builder.check_storage()
            vpy = bootstrap_tools_venv(out_dir)
            argv = [str(vpy), str(Path(__file__).resolve()), *sys.argv[1:], "--in-tools-venv"]
            os.execv(str(vpy), argv)
        builder.build()
    except BuildError as exc:
        log("ERROR: %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
