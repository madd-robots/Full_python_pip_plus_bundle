#!/usr/bin/env python3
"""Debian package stage of the MADD offline bundle builder (Python stdlib only).

This is the one stage that needs Debian/Ubuntu tooling (APT). It runs on a
Debian/Ubuntu host, or - when the builder runs in Termux - inside the existing
ubuntu-claude-secure proot environment, invoked by madd_stage.py.

It resolves and downloads the target's Debian package closure with an isolated
APT instance configured for the TARGET architecture (never the builder's),
and assembles a signed local repository slice:

    WORK/apt/<repo>/dists/<suite>/InRelease                  (archive-signed, as fetched)
    WORK/apt/<repo>/dists/<suite>/<comp>/binary-<arch>/Packages.xz  (hash-checked against InRelease)
    WORK/apt/<repo>/pool/...                                 (only the bundled .debs)
    WORK/apt/sources.txt    repo/suite/components list used by install.sh
    WORK/apt/preferences    pins: only the bundled versions are installable
    WORK/deb-stage.json     records, target facts, file listings, test results

The target verifies InRelease with its own installed archive keyring; nothing
here is marked trusted.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from madd_common import (  # noqa: E402
    BUILDER_DIR, BuildError, apt_lists_prefix, load_profile, parse_stanzas, pin_preferences,
    read_list_file, run as _run, sha256_file, upstream_version,
)

TAG = "madd-debstage"
RECORD_FIELDS = ("Depends", "Pre-Depends", "Recommends", "Suggests", "Provides", "Breaks",
                 "Enhances", "Section", "Description", "Tag", "Priority", "Source")


def log(msg):
    print("[%s] %s" % (TAG, msg), flush=True)


def run(cmd, **kw):
    kw.setdefault("tag", TAG)
    return _run(cmd, **kw)


class DebStage:
    def __init__(self, args):
        self.args = args
        self.profile = load_profile(args.profile)
        self.deb_arch = self.profile["DEB_ARCH"]
        self.work = Path(args.work).resolve()
        self.cache = Path(args.cache).resolve()
        self.tmp = self.work / "tmp"
        self.out = self.work / "apt"
        self.results = []
        self.target_state = Path(args.target_state).resolve() if args.target_state else None

    def record(self, name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        self.results.append((name, status, detail))
        log("TEST %-40s %s %s" % (name, status, detail))
        if not ok:
            raise BuildError("test failed: %s %s" % (name, detail))

    # -- keyring ------------------------------------------------------------
    def keyring(self):
        if self.args.keyring:
            kr = Path(self.args.keyring).resolve()
            if not kr.is_file():
                raise BuildError("keyring not found: %s" % kr)
            return kr
        system = Path(self.profile["STAGING_KEYRING"])
        if system.is_file():
            return system
        # Fetch the keyring package through the staging system's own signed APT
        # sources and extract it without root.
        pkg = self.profile["STAGING_KEYRING_PACKAGE"]
        kdir = self.cache / ("keyring-%s" % pkg)
        kdir.mkdir(parents=True, exist_ok=True)
        extracted = kdir / "root" / system.relative_to("/")
        if extracted.is_file():
            return extracted
        for old in kdir.glob("%s_*.deb" % pkg):
            old.unlink()
        proc = run(["apt-get", "download", pkg], cwd=kdir, capture=True, check=False)
        debs = sorted(kdir.glob("%s_*.deb" % pkg))
        if proc.returncode != 0 or len(debs) != 1:
            print(proc.stdout or "", flush=True)
            raise BuildError("could not obtain %s from the staging system's APT sources; run "
                             "'apt-get update' there or use --install-staging-prereqs" % pkg)
        run(["dpkg-deb", "-x", debs[0], kdir / "root"])
        if not extracted.is_file():
            raise BuildError("%s did not contain %s" % (pkg, system))
        return extracted

    # -- isolated APT ---------------------------------------------------------
    def write_apt_config(self, root, sources_text, lists_dir, archives_dir, prefs=""):
        root = Path(root)
        for sub in ("etc/apt.conf.d", "etc/sources.list.d", "etc/preferences.d",
                    "etc/trusted.gpg.d", "state", "log", "cache"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        Path(lists_dir, "partial").mkdir(parents=True, exist_ok=True)
        Path(archives_dir, "partial").mkdir(parents=True, exist_ok=True)
        (root / "state" / "status").write_text("")
        (root / "etc" / "sources.list.d" / "madd.sources").write_text(sources_text)
        (root / "etc" / "preferences.d" / "madd.pref").write_text(prefs)
        (root / "etc" / "sources.list").write_text("")
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
        return conf

    @staticmethod
    def apt_env(conf, offline=False):
        env = dict(os.environ, APT_CONFIG=str(conf), LC_ALL="C", DEBIAN_FRONTEND="noninteractive")
        if offline:
            for k in list(env):
                if k.lower().endswith("_proxy"):
                    del env[k]
            # Unreachable proxy: any accidental network fetch fails loudly.
            env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
        return env

    def repos(self):
        p = self.profile
        for rid in p["REPO_IDS"].split():
            yield rid, p["REPO_%s_URI" % rid.upper()], p["REPO_%s_SUITES" % rid.upper()].split()

    def online_sources(self, keyring):
        comps = self.profile["REPO_COMPONENTS"]
        return "\n".join(
            "Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nArchitectures: %s\nSigned-By: %s\n"
            % (uri, " ".join(suites), comps, self.deb_arch, keyring)
            for _rid, uri, suites in self.repos())

    def simulate(self, env, pkgs, status=None):
        """APT simulation (no changes) -> set of (name, version, arch) to install."""
        cmd = ["apt-get", "-s", "-q", "install", "--no-install-recommends"]
        if status is not None:
            cmd += ["-o", "Dir::State::status=%s" % status]
        proc = run(cmd + list(pkgs), env=env, capture=True, check=False, quiet=True)
        if proc.returncode != 0:
            print(proc.stdout[-4000:], flush=True)
            raise BuildError("APT could not resolve %s" % ("the target-state system" if status else "the package set"))
        return self.parse_apt_sim(proc.stdout)

    @staticmethod
    def parse_apt_sim(text):
        wanted = set()
        for line in text.splitlines():
            if line.startswith("Remv "):
                raise BuildError("unexpected removal in simulation: " + line)
            m = re.match(r"^Inst (\S+) (?:\[\S+\] )?\((\S+) .*\[(\S+)\]\)", line)
            if m:
                wanted.add((m.group(1).split(":")[0], m.group(2), m.group(3)))
        if not wanted:
            raise BuildError("APT simulation produced no packages")
        return wanted

    # -- resolution -------------------------------------------------------------
    def resolve(self):
        keyring = self.keyring()
        log("archive keyring: %s" % keyring)
        pid = self.profile["PROFILE_ID"]
        self.lists = self.cache / ("apt-lists-%s" % pid)
        self.archives = self.cache / ("apt-archives-%s" % pid)
        self.conf = self.write_apt_config(self.tmp / "apt", self.online_sources(keyring), self.lists, self.archives)
        env = self.apt_env(self.conf)
        run(["apt-get", "update", "-q"], env=env)

        self.deb_top = read_list_file(BUILDER_DIR / "debian-packages.txt")
        wanted = self.simulate(env, self.deb_top)
        log("dependency closure (empty system): %d packages" % len(wanted))
        empty_names = {n for n, _v, _a in wanted}
        forced = set()
        self.models = []
        if not self.args.skip_baseline_model:
            for label, status in self.baseline_models(keyring, env):
                ops = self.simulate(env, self.deb_top, status=status)
                forced |= {n for n, _v, _a in ops}
                log("baseline model %s: %d package operation(s)" % (label, len(ops)))
        self.lockstep = sorted(forced - empty_names)
        if self.lockstep:
            wanted = self.simulate(env, self.deb_top + self.lockstep)
            log("added %d base package(s) that older target systems must upgrade in lockstep: %s"
                % (len(self.lockstep), " ".join(self.lockstep)))
        if self.target_state:
            self.tstatus = self.tmp / "target-status"
            shutil.copyfile(self.target_state / "status", self.tstatus)
            tw = self.simulate(env, self.deb_top, status=self.tstatus)
            log("target-state resolution: %d package operation(s), %d not already in the bundle"
                % (len(tw), len({w for w in tw if w[0] not in {n for n, _v, _a in wanted}})))
            wanted |= tw
        names = sorted({n for n, _v, _a in wanted})
        run(["apt-get", "-q", "-y", "install", "--download-only", "--no-install-recommends", *names], env=env)
        self.wanted = wanted

    def baseline_models(self, keyring, cur_env):
        """Synthetic dpkg status files describing un-updated target machines.

        Built from the signed release-time index (BASELINE_URI/BASELINE_SUITES) and
        closed under its dependencies:
          M1: Essential/required/important/standard + BASELINE_EXTRA_PACKAGES
          M2: M1 + every bundle package at its release-time version (worst case)
        """
        p = self.profile
        root = self.tmp / "apt-baseline"
        sources = ("Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nArchitectures: %s\nSigned-By: %s\n"
                   % (p["BASELINE_URI"], p["BASELINE_SUITES"], p["REPO_COMPONENTS"], self.deb_arch, keyring))
        conf = self.write_apt_config(root, sources, self.cache / ("apt-lists-baseline-%s" % p["PROFILE_ID"]),
                                     root / "archives")
        env = self.apt_env(conf)
        # Archive snapshots are old by definition; their signatures are still verified.
        run(["apt-get", "update", "-q", "-o", "Acquire::Check-Valid-Until=false"], env=env)
        dump = run(["apt-cache", "dumpavail"], env=env, capture=True, quiet=True).stdout
        old, fields = {}, {}
        for f, st in parse_stanzas(dump):
            old[f["Package"]] = st
            fields[f["Package"]] = f

        def likely(f):
            return f.get("Essential") == "yes" or f.get("Priority") in ("required", "important", "standard")

        extra = set(p.get("BASELINE_EXTRA_PACKAGES", "").split())
        m1 = {n for n, f in fields.items() if likely(f)} | {n for n in extra if n in old}
        bundle_now = {n for n, _v, _a in self.simulate(cur_env, self.deb_top)}
        m2 = m1 | {n for n in bundle_now if n in old}
        for label, base in (("M1-standard+desktop", m1), ("M2-worst-case", m2)):
            closed = self.simulate(env, sorted(base))
            status = root / ("status-%s" % label)
            with open(status, "w", encoding="utf-8") as fh:
                for name, _v, _a in sorted(closed):
                    fh.write(old[name].strip("\n") + "\nStatus: install ok installed\n\n")
            log("baseline %s: %d installed packages at release-time versions" % (label, len(closed)))
            self.models.append((label, status))
        return list(self.models)

    # -- assemble signed local repository ---------------------------------------
    def load_index(self):
        """(pkg, ver, arch) -> list of (repo_id, fields) from the signature-verified lists."""
        index = {}
        prefixes = {apt_lists_prefix(uri): rid for rid, uri, _s in self.repos()}
        files = sorted(self.lists.glob("*_Packages"))
        if not files:
            raise BuildError("no Packages indexes in %s" % self.lists)
        for f in files:
            rid = None
            for pre, r in prefixes.items():
                if f.name.startswith(pre + "_dists_"):
                    rid = r
            if rid is None:
                continue
            for fields, _st in parse_stanzas(f.read_text(encoding="utf-8", errors="replace")):
                key = (fields.get("Package"), fields.get("Version"), fields.get("Architecture"))
                index.setdefault(key, []).append((rid, fields))
        return index

    @staticmethod
    def release_hashes(inrelease_text):
        """SHA256 section of a (clearsigned) InRelease: path -> (sha256, size)."""
        out = {}
        in_sha = False
        for line in inrelease_text.splitlines():
            if line.startswith("-----BEGIN PGP SIGNATURE-----"):
                break
            if re.match(r"^[A-Za-z0-9-]+:", line):
                in_sha = line.strip() == "SHA256:"
                continue
            if in_sha and line.startswith(" "):
                parts = line.split()
                if len(parts) == 3:
                    out[parts[2]] = (parts[0], int(parts[1]))
        return out

    def fetch_verified(self, url, dest, sha, size):
        dest.parent.mkdir(parents=True, exist_ok=True)
        cached = self.cache / "apt-index-cache" / sha
        if cached.is_file() and sha256_file(cached) == sha:
            shutil.copyfile(cached, dest)
            return
        log("fetch %s" % url)
        req = urllib.request.Request(url, headers={"User-Agent": "madd-python-bundle-builder"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:  # https, verified TLS
            shutil.copyfileobj(resp, fh)
        if dest.stat().st_size != size or sha256_file(dest) != sha:
            dest.unlink()
            raise BuildError("index file from %s does not match the signed InRelease" % url)
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest, cached)

    def assemble(self):
        self.out.mkdir(parents=True)
        index = self.load_index()
        comps = self.profile["REPO_COMPONENTS"].split()
        self.index_files = []
        self.origins = set()
        sources = []
        # 1. Signed metadata, exactly as APT verified it, plus the matching Packages.xz.
        for rid, uri, suites in self.repos():
            pre = apt_lists_prefix(uri)
            for suite in suites:
                src = self.lists / ("%s_dists_%s_InRelease" % (pre, suite))
                if not src.is_file():
                    raise BuildError("missing verified InRelease for %s %s" % (rid, suite))
                text = src.read_text(encoding="utf-8")
                m = re.search(r"(?m)^Origin: (.+)$", text)
                if not m:
                    raise BuildError("InRelease for %s %s has no Origin" % (rid, suite))
                self.origins.add(m.group(1).strip())
                dist = self.out / rid / "dists" / suite
                dist.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dist / "InRelease")
                self.index_files.append(dist / "InRelease")
                hashes = self.release_hashes(text)
                by_hash = re.search(r"(?m)^Acquire-By-Hash: yes$", text) is not None
                for comp in comps:
                    rel = "%s/binary-%s/Packages.xz" % (comp, self.deb_arch)
                    listed = self.lists / ("%s_dists_%s_%s_binary-%s_Packages" % (pre, suite, comp, self.deb_arch))
                    if not listed.is_file():
                        raise BuildError("APT did not use %s %s %s" % (rid, suite, rel))
                    if rel not in hashes:
                        raise BuildError("%s %s InRelease lacks %s" % (rid, suite, rel))
                    sha, size = hashes[rel]
                    # The uncompressed index APT verified must be the one this .xz decompresses to.
                    plain = hashes.get(rel[:-3])
                    if plain and sha256_file(listed) != plain[0]:
                        raise BuildError("APT lists for %s %s are not the signed version" % (rid, suite))
                    url = ("%s/dists/%s/%s/binary-%s/by-hash/SHA256/%s" % (uri, suite, comp, self.deb_arch, sha)
                           if by_hash else "%s/dists/%s/%s" % (uri, suite, rel))
                    dest = dist / rel
                    self.fetch_verified(url, dest, sha, size)
                    self.index_files.append(dest)
                sources.append("%s %s %s" % (rid, suite, " ".join(comps)))
        (self.out / "sources.txt").write_text("\n".join(sources) + "\n")

        # 2. The bundled .debs at their pool paths in every repository that lists them.
        self.records = []
        seen = set()
        for name, ver, arch in sorted(self.wanted):
            if arch not in (self.deb_arch, "all"):
                raise BuildError("resolved %s %s for wrong architecture %s" % (name, ver, arch))
            if name in seen:
                raise BuildError("duplicate package in closure: %s" % name)
            seen.add(name)
            locs = index.get((name, ver, arch))
            if not locs:
                raise BuildError("no signed index entry for %s %s %s" % (name, ver, arch))
            fields = locs[0][1]
            src = self.archives / ("%s_%s_%s.deb" % (name, ver.replace(":", "%3a"), arch))
            if not src.is_file():
                raise BuildError("APT did not download %s" % src.name)
            digest = sha256_file(src)
            size = src.stat().st_size
            pool = fields.get("Filename", "")
            if (not re.match(r"^pool/[A-Za-z0-9.+~_/-]+\.deb$", pool) or ".." in pool.split("/")):
                raise BuildError("unexpected Filename %r" % pool)
            paths = []
            for rid, f in locs:
                if f.get("SHA256") != digest or f.get("Size") != str(size):
                    raise BuildError("hash/size mismatch against signed index for %s (%s)" % (src.name, rid))
                dst = self.out / rid / f["Filename"]
                rel = dst.relative_to(self.out).as_posix()
                if rel in paths:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                paths.append(rel)
            contents = run(["dpkg-deb", "--contents", src], capture=True, quiet=True).stdout
            listing = []
            for line in contents.splitlines():
                parts = line.split(None, 5)
                if len(parts) == 6:
                    listing.append(parts[5].split(" -> ")[0])
            self.records.append({
                "name": name, "version": ver, "arch": arch, "path": paths[0], "copies": paths[1:],
                "sha256": digest, "size": size,
                "installed_kb": int(fields.get("Installed-Size", "0") or 0),
                "fields": {k: fields[k] for k in RECORD_FIELDS if k in fields},
                "contents": listing,
            })
        (self.out / "preferences").write_text(pin_preferences(self.records, self.origins))
        self.record("deb: closure downloaded, hash-verified against signed indexes", True,
                    "%d packages, all %s/all" % (len(self.records), self.deb_arch))

    # -- offline verification of the assembled slice ----------------------------
    def offline_tests(self, keyring):
        root = self.tmp / "apt-offline-test"
        if " " in str(self.out):
            raise BuildError("work path must not contain spaces")
        entries = []
        for line in (self.out / "sources.txt").read_text().split("\n"):
            if not line.strip():
                continue
            rid, suite, *comps = line.split()
            entries.append("Types: deb\nURIs: file:%s/%s\nSuites: %s\nComponents: %s\nArchitectures: %s\n"
                           "Signed-By: %s\nTargets: Packages\nCheck-Valid-Until: no\n"
                           % (self.out, rid, suite, " ".join(comps), self.deb_arch, keyring))
        conf = self.write_apt_config(root, "\n".join(entries), root / "lists", root / "archives",
                                     prefs=(self.out / "preferences").read_text())
        env = self.apt_env(conf, offline=True)
        # Builder-side self-test only: as (proot) root, read the private work tree
        # directly instead of handing it to the _apt sandbox user, which may not be
        # able to traverse the builder's directories. Signature checks are unaffected.
        sandbox = ["-o", "APT::Sandbox::User=root"] if os.geteuid() == 0 else []
        upd = run(["apt-get", "update", "-q", "-o", "Acquire::By-Hash=no"] + sandbox, env=env,
                  capture=True, check=False)
        problems = re.findall(r"(?m)^(?:Err|W:|E:).*$", upd.stdout)
        if upd.returncode != 0 or problems:
            print(upd.stdout[-3000:], flush=True)
        self.record("deb: signed local repository verifies offline (no trusted=yes)",
                    upd.returncode == 0 and not problems,
                    "; ".join(problems)[:400] if problems else "%d signed suites" % len(entries))
        expect = {(r["name"], r["version"], r["arch"]) for r in self.records}

        def sim(pkgs, status=None):
            cmd = ["apt-get", "-s", "-q", "install", "--no-install-recommends"]
            if status:
                cmd += ["-o", "Dir::State::status=%s" % status]
            p = run(cmd + list(pkgs), env=env, capture=True, check=False, quiet=True)
            if p.returncode != 0:
                print(p.stdout[-3000:], flush=True)
                return False, set(), p.stdout
            return True, self.parse_apt_sim(p.stdout), p.stdout

        ok, got, _o = sim(self.deb_top)
        self.record("deb: offline resolution from bundled versions only", ok and got <= expect,
                    "%d packages for an empty system" % len(got))
        ok, got, _o = sim(sorted(r["name"] for r in self.records))
        self.record("deb: every bundled package installable offline", ok and got == expect,
                    "%d/%d" % (len(got), len(expect)))
        for label, status in self.models:
            ok, got, out = sim(self.deb_top, status)
            self.record("deb: offline resolution, baseline %s" % label, ok and got <= expect,
                        "%d upgrade(s), %d new" % (len(re.findall(r"(?m)^Inst \S+ \[", out)),
                                                   len(re.findall(r"(?m)^Inst \S+ \(", out))))
        if self.target_state:
            ok, got, _o = sim(self.deb_top, self.tstatus)
            self.record("deb: offline resolution against exported target state", ok and got <= expect,
                        "%d operation(s)" % len(got))

    # -- target facts ------------------------------------------------------------
    def target_facts(self):
        by_name = {r["name"]: r for r in self.records}
        py, libc = by_name.get("python3"), by_name.get("libc6")
        if not py or not libc:
            raise BuildError("python3/libc6 missing from closure")
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", upstream_version(py["version"]))
        if not m:
            raise BuildError("cannot parse python3 version %r" % py["version"])
        short = "%s.%s" % (m.group(1), m.group(2))
        if "python%s" % short not in by_name:
            raise BuildError("python%s interpreter package missing from closure" % short)
        g = re.match(r"^(\d+)\.(\d+)", upstream_version(libc["version"]))
        return {
            "python_full": "%s.%s.%s" % m.groups(), "python_short": short,
            "python_major": int(m.group(1)), "python_minor": int(m.group(2)),
            "python_abi": "cp%s%s" % (m.group(1), m.group(2)),
            "python_deb_version": by_name["python%s" % short]["version"],
            "glibc": [int(g.group(1)), int(g.group(2))],
        }

    def main(self):
        if self.out.exists():
            raise BuildError("%s already exists" % self.out)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.resolve()
        self.assemble()
        keyring = self.keyring()
        self.offline_tests(keyring)
        facts = self.target_facts()
        index_files = [{"path": p.relative_to(self.out).as_posix(), "sha256": sha256_file(p),
                        "size": p.stat().st_size} for p in self.index_files]
        data = {
            "profile": self.profile["PROFILE_ID"], "deb_arch": self.deb_arch,
            "deb_top": self.deb_top, "lockstep": self.lockstep,
            "models": [label for label, _s in self.models],
            "target_state": bool(self.target_state), "records": self.records,
            "index_files": index_files, "origins": sorted(self.origins),
            "target": facts, "results": self.results,
            "keyring": "%s (sha256 %s)" % (keyring, sha256_file(keyring)),
            "environment": staging_label(),
        }
        tmp = self.work / "deb-stage.json.tmp"
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, self.work / "deb-stage.json")
        shutil.rmtree(self.tmp)
        log("deb stage complete: %d packages, Python %s, glibc %d.%d"
            % (len(self.records), facts["python_full"], *facts["glibc"]))


def staging_label():
    try:
        from madd_common import read_os_release
        pretty = read_os_release().get("PRETTY_NAME", "unknown")
    except OSError:
        pretty = "unknown"
    return "%s, %s (%s)" % (pretty, os.uname().machine, os.environ.get("MADD_STAGING_LABEL", "local"))


def main():
    ap = argparse.ArgumentParser(description="Debian package stage (needs APT)")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--work", required=True, help="empty directory for this stage's output")
    ap.add_argument("--cache", required=True, help="persistent APT list/archive cache")
    ap.add_argument("--target-state", default="")
    ap.add_argument("--keyring", default="")
    ap.add_argument("--skip-baseline-model", action="store_true")
    args = ap.parse_args()
    started = time.time()
    try:
        DebStage(args).main()
    except BuildError as exc:
        log("ERROR: %s" % exc)
        return 1
    log("done in %.0fs" % (time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
