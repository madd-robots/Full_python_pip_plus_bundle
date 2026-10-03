#!/usr/bin/env python3
"""Orchestrator of the MADD offline Python bundle builder.

Runs directly in Termux on Android (preferred) or on a Debian/Ubuntu host.
Everything that does not need Debian tooling happens here: target-wheel
resolution (explicit target platform/ABI tags, never the builder's own),
verification of every artifact, manifests and inventory, the wireless audit,
static checks (ShellCheck is mandatory), the archive and its self-tests.

The Debian package stage (APT) runs through builder/stage.sh, either locally
on a Debian/Ubuntu host or - from Termux - inside the existing proot-distro
Ubuntu environment (--deb-stage proot:NAME). Its output is re-verified here.

Phase 0 runs on the system python3 (stdlib only), builds a private tools venv
from hash-pinned wheels (no ensurepip needed) and re-executes itself there.
"""

import argparse
import datetime
import email.parser
import gzip
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from madd_common import (  # noqa: E402
    BUILDER_DIR, REPO_ROOT, STAGING_MARKER, TEMPLATE_DIR, BuildError, load_profile, log,
    read_list_file, read_os_release, run, safe_rmtree, safe_rmtree_child, sha256_file,
)

BUNDLE_FORMAT_VERSION = "2"
TOOLS_STAMP = ".madd-tools-stamp"
PYPI_INDEX = "https://pypi.org/simple"
PYPI_JSON = "https://pypi.org/pypi/%s/%s/json"
EXECUTABLE_FILES = {"install.sh", "verify.sh", "uninstall-user-env.sh", "export-target-state.sh"}
MNT_PROJECT = "/mnt/madd-python-project"
MNT_OUTPUT = "/mnt/madd-python-output"
MNT_TARGET_STATE = "/mnt/madd-python-target-state"


# ---------------------------------------------------------------------------
# Phase 0: private tools venv from hash-pinned wheels (stdlib only)
# ---------------------------------------------------------------------------

def parse_pins(req):
    """name==version --hash=sha256:... entries of tools-requirements.txt."""
    text = Path(req).read_text().replace("\\\n", " ")
    pins = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+-]+)\s+--hash=sha256:([0-9a-f]{64})", text)
    if not pins:
        raise BuildError("no pinned tools in %s" % req)
    return pins


def fetch_pinned_wheel(name, version, sha, dest_dir):
    """Download the wheel whose SHA-256 is pinned, via PyPI's JSON API over verified HTTPS."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in dest_dir.glob("*.whl"):
        if f.name.lower().startswith(name.lower().replace("-", "_") + "-" + version) and sha256_file(f) == sha:
            return f
    with urllib.request.urlopen(PYPI_JSON % (name, version), timeout=60) as resp:
        meta = json.load(resp)
    for u in meta.get("urls", []):
        if u.get("digests", {}).get("sha256") == sha and u["filename"].endswith(".whl"):
            if not re.match(r"^[A-Za-z0-9_.+-]+\.whl$", u["filename"]) or not u["url"].startswith("https://"):
                raise BuildError("unexpected PyPI file entry for %s" % name)
            dest = dest_dir / u["filename"]
            log("fetch %s" % u["url"])
            with urllib.request.urlopen(u["url"], timeout=120) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            if sha256_file(dest) != sha:
                dest.unlink()
                raise BuildError("hash mismatch for %s" % u["filename"])
            return dest
    raise BuildError("PyPI has no wheel of %s %s with the pinned SHA-256" % (name, version))


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
    run([sys.executable, "-m", "venv", "--without-pip", venv_dir])
    wheel_dir = cache / "tools-wheels"
    wheels = {name.lower(): fetch_pinned_wheel(name, ver, sha, wheel_dir) for name, ver, sha in parse_pins(req)}
    if "pip" not in wheels:
        raise BuildError("tools-requirements.txt must pin pip")
    env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")
    # Run pip from its own (hash-verified) wheel to install the pinned tools offline.
    run([vpy, wheels["pip"] / "pip", "install", "--isolated", "--no-index", "--no-cache-dir",
         "--require-hashes", "--only-binary=:all:", "--no-deps", "--find-links", wheel_dir, "-r", req], env=env)
    stamp.write_text(stamp_value + "\n")
    return vpy


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class Builder:
    def __init__(self, args):
        self.args = args
        self.out_dir = Path(args.output).resolve()
        self.profile = load_profile(args.profile)
        self.bundle_name = self.profile["BUNDLE_NAME"]
        self.deb_arch = self.profile["DEB_ARCH"]
        self.wheel_arch = self.profile["WHEEL_PLATFORM_ARCH"]
        self.cache = self.out_dir / "cache"
        self.dist = self.out_dir / "dist"
        self.results = []  # (test name, PASS/FAIL/SKIP, detail)
        self.build_epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())
        self.staging = None
        self.target_state = Path(args.target_state).resolve() if args.target_state else None

    def validate_target_state(self):
        """Check a directory produced by export-target-state.sh on the target machine."""
        d = self.target_state
        for n in ("status", "dpkg-architecture", "os-release"):
            if not (d / n).is_file():
                raise BuildError("target state %s lacks %s (create it with export-target-state.sh)" % (d, n))
        arch = (d / "dpkg-architecture").read_text().strip()
        if arch != self.deb_arch:
            raise BuildError("target state architecture %r does not match profile %r" % (arch, self.deb_arch))
        osr = read_os_release(d / "os-release")
        if osr.get("VERSION_CODENAME") not in ("", None, self.profile["TARGET_CODENAME"]) \
                and not (d / "mx-version").is_file():
            raise BuildError("target state is %s, profile expects %s"
                             % (osr.get("VERSION_CODENAME"), self.profile["TARGET_CODENAME"]))
        status = (d / "status").read_text(encoding="utf-8", errors="replace")
        if "Package: dpkg\n" not in status:
            raise BuildError("target state status file does not look like /var/lib/dpkg/status")
        log("target state: %s (%d installed-package records, sha256 %s)"
            % (d, status.count("\nPackage: ") + 1, sha256_file(d / "status")[:16]))

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
        for sub in ("wheels", "sdists"):
            (self.payload / sub).mkdir(parents=True)
        (self.bundle_dir / "scripts").mkdir(parents=True)
        log("staging directory: %s" % self.staging)

    def cleanup_staging(self):
        if self.staging and self.staging.exists():
            safe_rmtree(self.staging, self.out_dir, "staging.")

    # -- Debian package stage (APT: local, or ubuntu-claude-secure from Termux) --
    def deb_stage_command(self, work):
        mode = self.args.deb_stage
        extra = ["--profile", self.args.profile]
        if self.args.skip_baseline_model:
            extra.append("--skip-baseline-model")
        if self.args.install_staging_prereqs:
            extra.append("--install-staging-prereqs")
        if mode == "local":
            cmd = ["bash", BUILDER_DIR / "stage.sh", "--work", work, "--cache", self.cache] + extra
            if self.target_state:
                cmd += ["--target-state", self.target_state]
            if self.args.archive_keyring:
                cmd += ["--keyring", Path(self.args.archive_keyring).resolve()]
            return cmd
        m = re.match(r"^proot:([A-Za-z0-9][A-Za-z0-9._-]*)$", mode)
        if not m:
            raise BuildError("invalid --deb-stage %r" % mode)
        if self.args.archive_keyring:
            raise BuildError("--archive-keyring is only supported with a local Debian stage")
        distro = m.group(1)
        rel_work = Path(work).relative_to(self.out_dir).as_posix()
        binds = ["--bind", "%s:%s" % (REPO_ROOT, MNT_PROJECT), "--bind", "%s:%s" % (self.out_dir, MNT_OUTPUT)]
        inner = ["--work", "%s/%s" % (MNT_OUTPUT, rel_work), "--cache", "%s/cache" % MNT_OUTPUT] + extra
        if self.target_state:
            binds += ["--bind", "%s:%s" % (self.target_state, MNT_TARGET_STATE)]
            inner += ["--target-state", MNT_TARGET_STATE]
        return (["proot-distro", "login", distro] + binds +
                ["--", "/usr/bin/env", "MADD_STAGING_LABEL=proot-distro %s, used for the APT stage only" % distro,
                 "/bin/bash", "%s/builder/stage.sh" % MNT_PROJECT] + inner)

    def run_deb_stage(self):
        work = self.staging / "debstage"
        log("Debian package stage via %s" % self.args.deb_stage)
        run(self.deb_stage_command(work))
        result = work / "deb-stage.json"
        if not result.is_file():
            raise BuildError("Debian package stage produced no result")
        self.deb = json.loads(result.read_text())
        if self.deb.get("profile") != self.profile["PROFILE_ID"] or self.deb.get("deb_arch") != self.deb_arch:
            raise BuildError("Debian stage result is for a different profile/architecture")
        for name, status, detail in self.deb["results"]:
            if status != "PASS":
                raise BuildError("Debian stage test did not pass: %s" % name)
            self.results.append((name + " [apt stage]", status, detail))
        os.replace(work / "apt", self.payload / "apt")
        self.verify_deb_handoff()
        t = self.deb["target"]
        self.py_major, self.py_minor = t["python_major"], t["python_minor"]
        self.py_full, self.py_short, self.py_abi = t["python_full"], t["python_short"], t["python_abi"]
        self.python_deb_version = t["python_deb_version"]
        self.glibc = tuple(t["glibc"])
        self.deb_records = self.deb["records"]
        self.deb_top = self.deb["deb_top"]
        self.lockstep = self.deb["lockstep"]
        log("target Python: %s (%s), glibc %d.%d" % (self.py_full, self.py_abi, *self.glibc))

    def verify_deb_handoff(self):
        """Re-check every file that crossed the stage boundary before trusting it."""
        apt = self.payload / "apt"
        expected = {"sources.txt", "preferences"}
        bad = []
        for r in self.deb["records"]:
            if r["arch"] not in (self.deb_arch, "all"):
                bad.append("%s is %s" % (r["name"], r["arch"]))
            for rel in [r["path"]] + r["copies"]:
                expected.add(rel)
                f = apt / rel
                if not f.is_file() or f.is_symlink() or sha256_file(f) != r["sha256"]:
                    bad.append(rel)
        for ix in self.deb["index_files"]:
            expected.add(ix["path"])
            f = apt / ix["path"]
            if not f.is_file() or sha256_file(f) != ix["sha256"]:
                bad.append(ix["path"])
        present = {p.relative_to(apt).as_posix() for p in apt.rglob("*") if not p.is_dir()}
        bad += sorted(present - expected)
        self.record("deb: stage output re-verified after hand-off", not bad,
                    "; ".join(bad[:5]) if bad else "%d packages, %d signed index files"
                    % (len(self.deb["records"]), len(self.deb["index_files"])))

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
        # sdist-fallback.txt lines: NAME [manual-deps]
        fallback = [line.split() for line in read_list_file(BUILDER_DIR / "sdist-fallback.txt")]
        sdist_names = {canonicalize_name(f[0]) for f in fallback}
        self.sdist_manual = {canonicalize_name(f[0]) for f in fallback if "manual-deps" in f[1:]}
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
        from packaging.version import Version
        dynamic = {d.lower() for d in meta.get_all("Dynamic") or []}
        from packaging.utils import canonicalize_name
        manual = canonicalize_name(meta.get("Name", "")) in getattr(self, "sdist_manual", set())
        if not manual and (Version(meta.get("Metadata-Version", "1.0")) < Version("2.2") or "requires-dist" in dynamic):
            # Dependencies are only known after running the package's build code,
            # which cannot be done reliably for a foreign target here. Refuse rather
            # than guess (an incomplete wheelhouse would only fail on the target).
            raise BuildError(
                "%s does not declare static dependency metadata (Metadata-Version %s%s). "
                "List its runtime dependencies explicitly in builder/requirements.in, mark the "
                "entry 'NAME manual-deps' in builder/sdist-fallback.txt, and rerun; the listed "
                "dependencies are then resolved and audited like every other package."
                % (sdist.name, meta.get("Metadata-Version", "?"),
                   ", Requires-Dist dynamic" if "requires-dist" in dynamic else ""))
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
        self.closure_deps = {}
        top = [r for r in self.top_reqs if r.marker is None or r.marker.evaluate(dict(env, extra=""))]
        self.top_names = sorted({canonicalize_name(r.name) for r in top})
        queue = [(canonicalize_name(r.name), frozenset(r.extras), r) for r in top]
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
            deps = self.closure_deps.setdefault(name, set())
            for dep in meta_cache[name].get_all("Requires-Dist") or []:
                r = Requirement(dep)
                if r.marker is not None:
                    if not any(r.marker.evaluate(dict(env, extra=e)) for e in sorted(extras | {""})):
                        continue
                deps.add(canonicalize_name(r.name))
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
        closure = ["# canonical-name version sha256 required-distributions(comma-separated, target markers applied)"]
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
            closure.append("%s %s %s %s" % (name, rec["version"], digest,
                                            ",".join(sorted(self.closure_deps.get(name, ()))) or "-"))
        (b / "requirements.lock").write_text("\n".join(lock) + "\n")
        (b / "wheel-manifest.txt").write_text("\n".join(wm) + "\n")
        (b / "python-closure.txt").write_text("\n".join(closure) + "\n")
        (b / "python-top-level.txt").write_text("".join(n + "\n" for n in self.top_names))

        dm = ["# package version architecture path(under payload/apt) sha256 size installed-size-kb"]
        for r in sorted(self.deb_records, key=lambda r: r["name"]):
            dm.append("%s %s %s %s %s %d %d" % (r["name"], r["version"], r["arch"], r["path"],
                                                 r["sha256"], r["size"], r["installed_kb"]))
        (b / "deb-manifest.txt").write_text("\n".join(dm) + "\n")

        self.sdist_records = [r for r in self.wheel_records if r["kind"] == "sdists"]
        pipver = next((r["version"] for r in self.wheel_records if r["canonical"] == "pip"), "")
        p = self.profile
        info = [
            ("BUNDLE_NAME", self.bundle_name),
            ("BUNDLE_FORMAT_VERSION", BUNDLE_FORMAT_VERSION),
            ("PROFILE_ID", p["PROFILE_ID"]),
            ("TARGET_DISTRIBUTION", p["TARGET_DISTRIBUTION"]),
            ("TARGET_FAMILY", p["TARGET_FAMILY"]),
            ("TARGET_OS_ID", p["TARGET_OS_ID"]),
            ("TARGET_VERSION_ID", p["TARGET_VERSION_ID"]),
            ("TARGET_CODENAME", p["TARGET_CODENAME"]),
            ("TARGET_MX_RELEASE", p.get("TARGET_MX_RELEASE", "")),
            ("TARGET_KEYRING", p["TARGET_KEYRING"]),
            ("DEB_ARCH", self.deb_arch),
            ("UNAME_MACHINE", p["UNAME_MACHINE"]),
            ("PYTHON_VERSION", self.py_short),
            ("PYTHON_FULL_VERSION", self.py_full),
            ("PYTHON_DEB_VERSION", self.python_deb_version),
            ("PYTHON_ABI", self.py_abi),
            ("PIP_VERSION", pipver),
            ("GLIBC_VERSION", "%d.%d" % self.glibc),
            ("WHEEL_PLATFORM_MAX", self.wheel_platforms()[0]),
            ("DEB_TOP_LEVEL", " ".join(self.deb_top)),
            ("DEB_LOCKSTEP_BASE", " ".join(self.lockstep) or "none"),
            ("DEB_BASELINE_MODEL", "skipped" if self.args.skip_baseline_model else
             "%s [%s] (%s)" % (p["BASELINE_URI"], p["BASELINE_SUITES"], ", ".join(self.deb["models"]))),
            ("TARGET_STATE", ("exported dpkg status sha256 %s" % sha256_file(self.target_state / "status"))
             if self.target_state else "none (generic resolution for %s)" % p["TARGET_DISTRIBUTION"]),
            ("APT_ORIGINS", " ".join(self.deb["origins"])),
            ("APT_SOURCES", "; ".join("%s %s [%s]" % (rid, p["REPO_%s_URI" % rid.upper()],
                                                       p["REPO_%s_SUITES" % rid.upper()])
                                      for rid in p["REPO_IDS"].split()) + " (%s)" % p["REPO_COMPONENTS"]),
            ("DEB_COUNT", str(len(self.deb_records))),
            ("DEB_TOTAL_BYTES", str(sum(r["size"] for r in self.deb_records))),
            ("DEB_INSTALLED_SIZE_KB", str(sum(r["installed_kb"] for r in self.deb_records))),
            ("APT_PAYLOAD_BYTES", str(sum(f.stat().st_size for f in (self.payload / "apt").rglob("*") if f.is_file()))),
            ("WHEEL_COUNT", str(sum(1 for r in self.wheel_records if r["kind"] == "wheels"))),
            ("SDIST_COUNT", str(len(self.sdist_records))),
            ("PYDIST_TOTAL_BYTES", str(sum(r["size"] for r in self.wheel_records))),
            ("PYTHON_INDEX", PYPI_INDEX),
            ("BUILD_DATE_UTC", datetime.datetime.fromtimestamp(self.build_epoch, datetime.timezone.utc)
             .strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("BUILD_EPOCH", str(self.build_epoch)),
            ("BUILDER_ENVIRONMENT", self.args.builder_env),
            ("BUILDER_HOST_ARCH", platform.machine()),
            ("BUILDER_PYTHON", "%s %s" % (platform.python_implementation(), platform.python_version())),
            ("DEB_STAGE_ENVIRONMENT", self.deb["environment"]),
            ("DEB_STAGE_KEYRING", self.deb["keyring"]),
        ]
        for k, v in info:
            if "\n" in v:
                raise BuildError("newline in BUILD_INFO value %s" % k)
        (b / "BUILD_INFO.txt").write_text("".join("%s=%s\n" % kv for kv in info))
        self.write_inventory(dict(info))

    def write_inventory(self, info):
        """Human-readable inventory: what is inside, for which target, built where."""
        b = self.bundle_dir
        lines = [
            "MADD offline Python bundle inventory: %s" % info["BUNDLE_NAME"],
            "(Timestamps are informational; integrity is SHA256SUMS, verified by verify.sh.)",
            "",
            "Target distribution:  %s" % info["TARGET_DISTRIBUTION"],
            "Target family:        %s (os-release ID=%s VERSION_ID=%s codename %s)"
            % (info["TARGET_FAMILY"], info["TARGET_OS_ID"], info["TARGET_VERSION_ID"], info["TARGET_CODENAME"]),
            "Target architecture:  %s (uname -m %s)" % (info["DEB_ARCH"], info["UNAME_MACHINE"]),
            "Python:               %s (%s), Debian package %s" % (info["PYTHON_FULL_VERSION"], info["PYTHON_ABI"],
                                                                 info["PYTHON_DEB_VERSION"]),
            "pip:                  %s" % info["PIP_VERSION"],
            "Builder architecture: %s" % info["BUILDER_HOST_ARCH"],
            "Builder environment:  %s" % info["BUILDER_ENVIRONMENT"],
            "Builder Python:       %s" % info["BUILDER_PYTHON"],
            "APT stage:            %s" % info["DEB_STAGE_ENVIRONMENT"],
            "Built (UTC):          %s" % info["BUILD_DATE_UTC"],
            "",
            "Debian packages (%s):" % info["DEB_COUNT"],
        ]
        for r in sorted(self.deb_records, key=lambda r: r["name"]):
            lines.append("  %-36s %-34s %-5s sha256 %s" % (r["name"], r["version"], r["arch"], r["sha256"]))
        lines += ["", "Python distributions (%d):" % len(self.wheel_records)]
        for w in sorted(self.wheel_records, key=lambda w: w["canonical"]):
            lines.append("  %-28s %-14s %s sha256 %s" % (w["name"], w["version"], w["file"], w["sha256"]))
        lines += ["", "Signed APT metadata (verified on the target with %s):" % info["TARGET_KEYRING"]]
        for ix in self.deb["index_files"]:
            lines.append("  %s sha256 %s" % (ix["path"], ix["sha256"]))
        (b / "inventory.txt").write_text("\n".join(lines) + "\n")

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
            for path in r["contents"]:
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
        # ShellCheck is a required release check: a missing binary fails the build.
        if not shutil.which("shellcheck"):
            self.record("static: shellcheck (warning level)", False,
                        "shellcheck not installed; install it (Termux: pkg install shellcheck; "
                        "Debian/Ubuntu: apt-get install shellcheck) or rerun with --install-staging-prereqs")
        proc = run(["shellcheck", "-x", "-S", "warning", *shells], check=False, capture=True)
        if proc.stdout:
            print(proc.stdout, flush=True)
        self.record("static: shellcheck (warning level)", proc.returncode == 0, "")
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

    # -- offline resolution test ----------------------------------------
    def offline_pip_test(self):
        """Offline, hash-checked resolution of requirements.lock from the wheelhouse only."""
        dest = self.staging / "pip-offline-test"
        dest.mkdir()
        env = self.pip_env()
        for k in list(env):
            if k.lower().endswith("_proxy"):
                del env[k]
        env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
        base = [sys.executable, "-m", "pip", "download", "--isolated", "--no-index", "--no-cache-dir",
                "--find-links", self.payload / "wheels", "--find-links", self.payload / "sdists",
                "--require-hashes", "--dest", dest, "--progress-bar", "off"]
        sdist_names = {r["canonical"] for r in self.sdist_records}
        lock_lines = (self.bundle_dir / "requirements.lock").read_text().split("\n")
        entries = []
        for line in lock_lines:
            if line and not line.startswith(("#", " ")):
                entries.append([line])
            elif line.startswith(" ") and entries:
                entries[-1].append(line)
        from packaging.utils import canonicalize_name
        wheel_lock = self.staging / "lock-wheels.txt"
        sdist_lock = self.staging / "lock-sdists.txt"
        wheel_lock.write_text("\n".join("\n".join(e) for e in entries
                                         if canonicalize_name(e[0].split("==")[0]) not in sdist_names) + "\n")
        sdist_lock.write_text("\n".join("\n".join(e) for e in entries
                                         if canonicalize_name(e[0].split("==")[0]) in sdist_names) + "\n")
        # Wheels: explicit target tags (never the staging interpreter's own).
        cmd = base + ["--only-binary=:all:"] + self.pip_target_args() + ["-r", wheel_lock]
        if self.sdist_records:
            cmd.append("--no-deps")  # closure already verified with target markers
        proc = run(cmd, env=env, check=False, capture=True)
        detail = proc.stdout[-500:] if proc.returncode else ""
        ok = proc.returncode == 0
        if ok and self.sdist_records:
            # Preparing each sdist offline proves its build requirements are bundled.
            sproc = run(base + ["--no-binary=" + ",".join(sorted(sdist_names)), "--no-deps", "-r", sdist_lock],
                        env=env, check=False, capture=True)
            ok = sproc.returncode == 0
            detail = sproc.stdout[-500:] if not ok else ""
        n = len(list(dest.iterdir()))
        self.record("wheel: offline hash-checked resolution of lock", ok and n == len(self.wheel_records),
                    detail or "%d/%d distributions (%d sdist)" % (n, len(self.wheel_records), len(self.sdist_records)))

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

        def verify(label, expect_ok):
            p = run(["bash", "verify.sh"], cwd=root, check=False, capture=True)
            if expect_ok:
                print(p.stdout[-1800:], flush=True)
            ok = (p.returncode == 0) if expect_ok else (p.returncode != 0 and "VERIFY: PASS" not in p.stdout)
            self.record(label, ok, "exit %d" % p.returncode)

        verify("verify.sh: pass on pristine extraction", True)
        # Negative tests on the throwaway extraction (each on top of the previous one).
        sums = (root / "SHA256SUMS").read_text()
        with open(root / "README.md", "ab") as fh:
            fh.write(b"tampered\n")
        verify("verify.sh: rejects modified file", False)
        (root / "README.md").write_bytes(b"")
        victim = sorted((root / "payload" / "wheels").glob("*.whl"))[0]
        victim.unlink()
        verify("verify.sh: rejects missing file", False)
        # A dependency wheel removed consistently from wheelhouse, lock, manifests and
        # SHA256SUMS: only the dependency-closure check can notice.
        safe_rmtree_child(ext, self.staging)
        ext.mkdir()
        run(["tar", "-xzf", self.tar_path, "-C", ext])
        self.remove_dist_consistently(root, "idna")
        verify("verify.sh: rejects consistently removed dependency wheel", False)
        safe_rmtree_child(ext, self.staging)
        del sums

    @staticmethod
    def remove_dist_consistently(root, canonical):
        man = (root / "wheel-manifest.txt").read_text().splitlines()
        line = next(l for l in man if l.lower().startswith(canonical + " "))
        fname, digest = line.split()[3], line.split()[4]
        (root / "payload" / "wheels" / fname).unlink()
        (root / "wheel-manifest.txt").write_text("\n".join(l for l in man if l != line) + "\n")
        lock = (root / "requirements.lock").read_text()
        lock = re.sub(r"(?m)^\S+==\S+ \\\n    --hash=sha256:%s\n" % digest, "", lock)
        (root / "requirements.lock").write_text(lock)
        clo = (root / "python-closure.txt").read_text().splitlines()
        (root / "python-closure.txt").write_text("\n".join(l for l in clo if not l.startswith(canonical + " ")) + "\n")
        bi = (root / "BUILD_INFO.txt").read_text()
        n = int(re.search(r"(?m)^WHEEL_COUNT=(\d+)$", bi).group(1))
        (root / "BUILD_INFO.txt").write_text(re.sub(r"(?m)^WHEEL_COUNT=\d+$", "WHEEL_COUNT=%d" % (n - 1), bi))
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
        (root / "SHA256SUMS").write_text("".join("%s  %s\n" % (sha256_file(f), f.relative_to(root).as_posix())
                                                 for f in files))

    def finalize(self):
        digest = sha256_file(self.tar_path)
        name = self.tar_path.name
        sha_tmp = self.staging / (name + ".sha256")
        sha_tmp.write_text("%s  %s\n" % (digest, name))
        report = self.staging / ("%s.build-report.txt" % self.bundle_name)
        skips = [r for r in self.results if r[1] != "PASS"]
        lines = ["Build report for %s" % name, "sha256 %s" % digest,
                 "size_bytes %d" % self.tar_path.stat().st_size, ""]
        lines += ["%-62s %s %s" % r for r in self.results]
        lines += ["", "BUILD_INFO:"] + (self.bundle_dir / "BUILD_INFO.txt").read_text().splitlines()
        report.write_text("\n".join(lines) + "\n")
        if skips:
            raise BuildError("non-PASS results: %s" % skips)
        self.dist.mkdir(parents=True, exist_ok=True)
        # Atomic replacement: the previous bundle stays until the new one is validated.
        for f in (self.tar_path, sha_tmp, report, self.bundle_dir / "wireless-audit.txt",
                  self.bundle_dir / "inventory.txt"):
            target = self.dist / (f.name if f.parent == self.staging else "%s.%s" % (self.bundle_name, f.name))
            tmp = self.dist / (".incoming." + target.name)
            if f.parent == self.staging:
                os.replace(f, tmp)
            else:
                shutil.copy2(f, tmp)
            os.replace(tmp, target)
        info = dict(l.split("=", 1) for l in (self.bundle_dir / "BUILD_INFO.txt").read_text().splitlines())
        print("", flush=True)
        log("=" * 60)
        log("FINAL ARCHIVE:       %s" % (self.dist / name))
        log("SHA-256:             %s" % digest)
        log("SIZE:                %.1f MiB" % ((self.dist / name).stat().st_size / (1 << 20)))
        log("TARGET:              %s, %s" % (info["TARGET_DISTRIBUTION"], info["DEB_ARCH"]))
        log("BUILDER:             %s, %s" % (info["BUILDER_ENVIRONMENT"], info["BUILDER_HOST_ARCH"]))
        log("PYTHON:              %s (pip %s)" % (info["PYTHON_FULL_VERSION"], info["PIP_VERSION"]))
        log("DEBIAN PACKAGES:     %s" % info["DEB_COUNT"])
        log("PYTHON WHEELS:       %s (+%s sdist)" % (info["WHEEL_COUNT"], info["SDIST_COUNT"]))
        log("TESTS:               %d PASS, 0 FAIL, 0 SKIP" % len(self.results))
        log("=" * 60)

    def build(self):
        if self.target_state:
            self.validate_target_state()
        self.check_storage()
        self.make_staging()
        ok = False
        try:
            self.run_deb_stage()
            self.resolve_wheels()
            self.copy_templates()
            self.write_manifests()
            self.wireless_audit()
            self.static_checks()
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


def main():
    ap = argparse.ArgumentParser(description="MADD offline Python bundle builder (orchestrator)")
    ap.add_argument("--output", required=True, help="output directory (cache/, dist/, staging.*)")
    ap.add_argument("--profile", default="mx25-amd64")
    ap.add_argument("--deb-stage", required=True, help="'local' (Debian/Ubuntu host) or 'proot:NAME' (from Termux)")
    ap.add_argument("--builder-env", required=True, help="builder environment label (for the manifest)")
    ap.add_argument("--min-free-gb", default="4")
    ap.add_argument("--archive-keyring", default="")
    ap.add_argument("--target-state", default="", help="directory from export-target-state.sh")
    ap.add_argument("--skip-baseline-model", action="store_true",
                    help="do not model un-updated targets (not recommended)")
    ap.add_argument("--install-staging-prereqs", action="store_true")
    ap.add_argument("--keep-staging", action="store_true")
    ap.add_argument("--check-storage-only", action="store_true")
    ap.add_argument("--in-tools-venv", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
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
