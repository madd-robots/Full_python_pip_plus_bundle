#!/usr/bin/env python3
"""Installer test harness for a disposable Debian 13 chroot (run as root on a test host).

Prepare a chroot first, for example:
    mmdebstrap --variant=minbase trixie /var/tmp/madd-test/rootfs-min
    mmdebstrap --variant=minbase --include=python3,sudo,xvfb,xauth trixie /var/tmp/madd-test/rootfs-mx

Then:
    tests/test_install_chroot.py --rootfs /var/tmp/madd-test/rootfs-min --tarball output/dist/...tar.gz

The installer always runs inside a network namespace with no network
(unshare -n), so any network access attempt would fail. The harness never
deletes the chroot; remove it yourself after it has been unmounted.
"""

import argparse
import gzip
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

RESULTS = []
USER = "tester"


def sh(cmd, check=True, capture=True, stdin=None):
    proc = subprocess.run([str(c) for c in cmd], capture_output=capture, text=True, input=stdin)
    if check and proc.returncode != 0:
        print(proc.stdout[-4000:] if proc.stdout else "", proc.stderr[-4000:] if proc.stderr else "")
        raise SystemExit("harness command failed: %s" % cmd)
    return proc


def record(name, ok, detail=""):
    RESULTS.append((name, "PASS" if ok else "FAIL", detail))
    print("TEST %-58s %s %s" % (name, "PASS" if ok else "FAIL", detail), flush=True)


class Chroot:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if self.root == Path("/") or not (self.root / "etc/debian_version").is_file():
            raise SystemExit("not a Debian chroot: %s" % self.root)
        self.mounted = []

    def mount(self):
        for src, dst, opts in (("/dev", "dev", ["--bind"]), ("proc", "proc", ["-t", "proc"]),
                               ("tmpfs", "dev/shm", ["-t", "tmpfs"])):
            target = self.root / dst
            if sh(["mountpoint", "-q", target], check=False).returncode != 0:
                sh(["mount", *opts, src, target])
                self.mounted.append(target)

    def umount(self):
        for target in reversed(self.mounted):
            sh(["umount", target], check=False)
        self.mounted = []

    def run(self, argv, user=None, env=None, net=False):
        base = ["chroot", str(self.root)]
        if not net:
            base = ["unshare", "-n"] + base
        envv = ["env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C.UTF-8"]
        envv += ["%s=%s" % kv for kv in (env or {}).items()]
        if user:
            cmd = base + ["runuser", "-u", user, "--"] + envv + ["HOME=/home/%s" % user] + list(argv)
        else:
            cmd = base + envv + ["HOME=/root"] + list(argv)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def regen_sums(bundle):
    files = sorted(p for p in bundle.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (bundle / "SHA256SUMS").write_text("".join("%s  %s\n" % (sha(f), f.relative_to(bundle).as_posix()) for f in files))


def set_kv(path, key, value):
    text = path.read_text()
    text = re.sub(r"(?m)^%s=.*$" % key, "%s=%s" % (key, value), text)
    path.write_text(text)


def remove_deb(bundle, pkg):
    """Drop one dependency from a bundle copy and make all bundle metadata consistent again.

    The archive-signed Packages.xz still lists the package, but the pins only
    allow bundled versions, so APT must find it unresolvable.
    """
    man = bundle / "deb-manifest.txt"
    lines = man.read_text().splitlines(True)
    victim = [l for l in lines if l.startswith(pkg + " ")]
    assert len(victim) == 1, pkg
    fname = Path(victim[0].split()[3]).name
    for f in (bundle / "payload/apt").rglob(fname):
        f.unlink()
    man.write_text("".join(l for l in lines if l not in victim))
    prefs = bundle / "payload/apt/preferences"
    prefs.write_text(re.sub(r"(?m)^Package: %s\nPin: version \S+\nPin-Priority: 500\n\n?" % re.escape(pkg), "",
                            prefs.read_text()))
    n = sum(1 for l in man.read_text().splitlines() if not l.startswith("#"))
    set_kv(bundle / "BUILD_INFO.txt", "DEB_COUNT", str(n))
    regen_sums(bundle)


def remove_wheel(bundle, canonical, hide_from_closure=False):
    """Remove one Python distribution consistently from wheelhouse, lock, manifests.

    With hide_from_closure the dependency lists are scrubbed too, so verify.sh
    passes and only pip itself (offline, --no-index) can notice.
    """
    man = (bundle / "wheel-manifest.txt").read_text().splitlines()
    line = next(l for l in man if l.lower().startswith(canonical + " "))
    fname, digest = line.split()[3], line.split()[4]
    (bundle / "payload/wheels" / fname).unlink()
    (bundle / "wheel-manifest.txt").write_text("\n".join(l for l in man if l != line) + "\n")
    lock = bundle / "requirements.lock"
    lock.write_text(re.sub(r"(?m)^\S+==\S+ \\\n    --hash=sha256:%s\n" % digest, "", lock.read_text()))
    clo = [l for l in (bundle / "python-closure.txt").read_text().splitlines() if not l.startswith(canonical + " ")]
    if hide_from_closure:
        fixed = []
        for l in clo:
            parts = l.split()
            if not l.startswith("#") and len(parts) == 4:
                deps = [d for d in parts[3].split(",") if d != canonical]
                parts[3] = ",".join(deps) or "-"
                l = " ".join(parts)
            fixed.append(l)
        clo = fixed
        top = bundle / "python-top-level.txt"
        top.write_text("".join(n + "\n" for n in top.read_text().split() if n != canonical))
    (bundle / "python-closure.txt").write_text("\n".join(clo) + "\n")
    bi = (bundle / "BUILD_INFO.txt").read_text()
    n = int(re.search(r"(?m)^WHEEL_COUNT=(\d+)$", bi).group(1))
    set_kv(bundle / "BUILD_INFO.txt", "WHEEL_COUNT", str(n - 1))
    regen_sums(bundle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rootfs", required=True)
    ap.add_argument("--tarball", required=True)
    ap.add_argument("--use-sudo", action="store_true", help="invoke install.sh through sudo as the test user")
    ap.add_argument("--gui", action="store_true", help="start Xvfb in the chroot for the Tk display test")
    ap.add_argument("--foreign-tarball", help="bundle for another distribution; must be refused here")
    args = ap.parse_args()

    tarball = Path(args.tarball).resolve()
    name = tarball.name[:-len(".tar.gz")]
    ch = Chroot(args.rootfs)
    home = ch.root / "home" / USER
    ch.mount()
    try:
        if not home.exists():
            sh(["chroot", ch.root, "useradd", "-m", "-s", "/bin/bash", USER])
        if args.use_sudo:
            (ch.root / "etc/sudoers.d/tester").write_text("%s ALL=(ALL) NOPASSWD: ALL\n" % USER)
            (ch.root / "etc/sudoers.d/tester").chmod(0o440)
        for f in (tarball, Path(str(tarball) + ".sha256")):
            shutil.copy2(f, home / f.name)
        sh(["chroot", ch.root, "chown", "-R", "%s:%s" % (USER, USER), "/home/%s" % USER])
        b = "/home/%s/%s" % (USER, name)
        hb = home / name
        if hb.exists():
            # Stale extraction from an earlier run inside the disposable chroot.
            assert hb.resolve().parent == home.resolve(), hb
            shutil.rmtree(hb)

        uhome = "/home/%s" % USER
        rc, out = ch.run(["env", "-C", uhome, "sha256sum", "-c", name + ".tar.gz.sha256"], user=USER)
        rc2, out2 = ch.run(["env", "-C", uhome, "tar", "-xzf", name + ".tar.gz"], user=USER)
        record("user: archive sha256 check + extraction", rc == 0 and rc2 == 0, out.strip())

        apt_before = {p: sha(p) for p in (ch.root / "etc/apt").rglob("*") if p.is_file()}
        status_before = sha(ch.root / "var/lib/dpkg/status")

        rc, out = ch.run([b + "/verify.sh"], user=USER)
        record("user: verify.sh", rc == 0 and "VERIFY: PASS" in out)

        rc, out = ch.run([b + "/install.sh", "--preflight-only"], user=USER)
        record("user: install.sh --preflight-only (non-root, APT simulation)", rc == 0 and "simulation OK" in out,
               (re.search(r"simulation OK.*", out) or re.search(r"STOP.*", out) or [""])[0])
        if rc != 0:
            print(out[-3000:])

        # --- negative tests on modified copies -----------------------------
        def copy(tag):
            dst = home / ("%s-%s" % (name, tag))
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(hb, dst, symlinks=True)
            return dst, "/home/%s/%s" % (USER, dst.name)

        hc, cb = copy("corrupt")
        victim = sorted((hc / "payload/apt").rglob("libmpc3_*.deb"))[0]
        data = bytearray(victim.read_bytes())
        data[-100] ^= 0xFF
        victim.write_bytes(bytes(data))
        rc, out = ch.run([cb + "/install.sh", "--preflight-only", "--target-user", USER])
        record("reject: corrupted .deb (hash mismatch)", rc != 0 and "integrity check failed" in out)

        hw, wb = copy("wrongarch")
        set_kv(hw / "BUILD_INFO.txt", "DEB_ARCH", "arm64")
        set_kv(hw / "BUILD_INFO.txt", "UNAME_MACHINE", "aarch64")
        regen_sums(hw)
        rc, out = ch.run([wb + "/install.sh", "--preflight-only", "--target-user", USER])
        record("reject: wrong target architecture", rc != 0 and "architecture mismatch" in out)

        hm, mb = copy("missingdep")
        remove_deb(hm, "libmpc3")
        vrc, _vout = ch.run([mb + "/verify.sh"], user=USER)
        rc, out = ch.run([mb + "/install.sh", "--preflight-only", "--target-user", USER])
        record("reject: missing dependency (consistent bundle, APT simulation)",
               vrc == 0 and rc != 0 and "dependency resolution failed" in out and "libmpc3" in out)

        hp, pb = copy("missingwheel")
        remove_wheel(hp, "idna")
        rc, out = ch.run([pb + "/install.sh", "--preflight-only", "--target-user", USER])
        record("reject: missing mandatory wheel (closure check)",
               rc != 0 and "missing from the wheelhouse: idna" in out and "integrity check failed" in out)

        family = re.search(r"(?m)^TARGET_FAMILY=(\S+)$", (hb / "BUILD_INFO.txt").read_text()).group(1)
        real_osr = (ch.root / "etc/os-release").resolve()
        saved = real_osr.read_text()
        dv = ch.root / "etc/debian_version"
        dv_saved = dv.read_text()
        if family == "ubuntu":
            real_osr.write_text(saved.replace('VERSION_ID="24.04"', 'VERSION_ID="22.04"').replace("noble", "jammy"))
            label = "reject: unsupported distribution (Ubuntu 22.04)"
        else:
            real_osr.write_text(saved.replace('VERSION_ID="13"', 'VERSION_ID="12"').replace("trixie", "bookworm"))
            dv.write_text("12.11\n")
            label = "reject: unsupported distribution (Debian 12)"
        try:
            rc, out = ch.run([b + "/install.sh", "--preflight-only", "--target-user", USER])
            record(label, rc != 0 and ("mismatch" in out or "requires Debian" in out))
        finally:
            real_osr.write_text(saved)
            dv.write_text(dv_saved)

        if args.foreign_tarball:
            ft = Path(args.foreign_tarball).resolve()
            fname = ft.name[:-len(".tar.gz")]
            shutil.copy2(ft, home / ft.name)
            sh(["chroot", ch.root, "chown", "%s:%s" % (USER, USER), "/home/%s/%s" % (USER, ft.name)])
            ch.run(["env", "-C", uhome, "tar", "-xzf", ft.name], user=USER)
            rc, out = ch.run(["%s/%s/install.sh" % (uhome, fname), "--preflight-only", "--target-user", USER])
            record("reject: bundle built for another distribution (%s)" % fname,
                   rc != 0 and re.search(r"unsupported distribution|mismatch|MX Linux detected", out) is not None)
            shutil.rmtree(home / fname)
            (home / ft.name).unlink()

        for d in (hc, hw, hm, hp):
            shutil.rmtree(d)
        record("no system changes from rejected runs",
               sha(ch.root / "var/lib/dpkg/status") == status_before)

        # --- pip network fallback is impossible ----------------------------------
        # A wheel removed so consistently that verify.sh passes, a hostile pip.conf
        # and PIP_INDEX_URL: pip must fail offline (--no-index), never reach out.
        pipconf = home / ".config/pip/pip.conf"
        pipconf.parent.mkdir(parents=True, exist_ok=True)
        pipconf.write_text("[global]\nindex-url = https://pypi.org/simple\nno-index = false\n")
        sh(["chroot", ch.root, "chown", "-R", "%s:%s" % (USER, USER), "/home/%s/.config" % USER])
        hf, fb = copy("pipfallback")
        remove_wheel(hf, "idna", hide_from_closure=True)
        vrc, _vout = ch.run([fb + "/verify.sh"], user=USER)
        if args.use_sudo:
            rc, out = ch.run(["sudo", fb + "/install.sh", "--yes"], user=USER, env={"PIP_INDEX_URL": "https://pypi.org/simple"})
        else:
            rc, out = ch.run([fb + "/install.sh", "--yes"], env={"SUDO_USER": USER, "PIP_INDEX_URL": "https://pypi.org/simple"})
        marker = home / ".venvs/madd-python/.madd-python-bundle"
        record("pip network fallback impossible (missing wheel, hostile pip.conf, no network)",
               vrc == 0 and rc != 0 and re.search(r"No matching distribution found for idna|Could not find a version that "
                                                  r"satisfies the requirement idna", out) is not None
               and not re.search(r"NewConnectionError|Retrying \(|Max retries exceeded", out)
               and marker.is_file() and "status=incomplete" in marker.read_text())
        shutil.rmtree(hf)

        # --- real installation ------------------------------------------------
        env = {}
        if args.gui:
            subprocess.Popen(["unshare", "-n", "chroot", str(ch.root), "runuser", "-u", USER, "--",
                              "Xvfb", ":99", "-nolisten", "tcp", "-ac"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            env["DISPLAY"] = ":99"
        if args.use_sudo:
            inst = ["sudo", "--preserve-env=DISPLAY", b + "/install.sh", "--yes"] if args.gui else \
                ["sudo", b + "/install.sh", "--yes"]
            rc, out = ch.run(inst, user=USER, env=env)
        else:
            rc, out = ch.run([b + "/install.sh", "--yes"], env=dict(env, SUDO_USER=USER))
        Path("/var/tmp/madd-test/install-%s.log" % ch.root.name).write_text(out)
        record("install: offline install.sh (no network namespace), recovers from failed pip step",
               rc == 0 and "smoke tests: PASS" in out and "status=complete" in marker.read_text(),
               (re.search(r"simulation OK.*", out) or [""])[0])
        if rc != 0:
            print(out[-6000:])
        gui = re.search(r"(PASS|INFO)\s+tkinter GUI\s+(.*)", out)
        record("install: tkinter GUI test outcome reported", bool(gui), gui.group(0).strip() if gui else "")

        rc, out = ch.run(["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${Package}\n"])
        top = (hb / "deb-install-list.txt").read_text().split()
        inst_ok = all(re.search(r"(?m)^ii\s+%s$" % re.escape(p), out) for p in top)
        record("install: all requested Debian packages installed", inst_ok)
        apt_after = {p: sha(p) for p in (ch.root / "etc/apt").rglob("*") if p.is_file()}
        record("install: /etc/apt unchanged", apt_after == apt_before)
        left = list((ch.root / "var/tmp").glob("madd-python-install.*")) + list((ch.root / "tmp").glob("madd-*"))
        record("install: temporary work directories removed", not left, str(left))
        venv = home / ".venvs/madd-python"
        record("install: venv owned by user", venv.is_dir() and venv.stat().st_uid != 0 and
               (venv / ".madd-python-bundle").is_file())
        rc, out = ch.run(["/home/%s/.local/bin/madd-python" % USER, "-c",
                          "import numpy, scipy, pandas, tkinter, cryptography, serial; print('launcher-ok')"], user=USER)
        record("launcher: madd-python imports", rc == 0 and "launcher-ok" in out)
        rc, out = ch.run(["/home/%s/.local/bin/madd-pip" % USER, "--version"], user=USER)
        record("launcher: madd-pip", rc == 0 and "pip" in out)
        rc, out = ch.run(["/home/%s/.local/bin/madd-python-env" % USER, "python", "-c",
                          "import sys; print(sys.prefix)"], user=USER)
        record("launcher: madd-python-env runs inside venv", rc == 0 and ".venvs/madd-python" in out)
        rc, out = ch.run(["/usr/bin/python3", "-m", "pip", "install", "--user", "--no-index", "six"], user=USER)
        record("system python still externally managed (PEP 668)", rc != 0 and "externally-managed" in out)

        # --- rerun / idempotency ----------------------------------------------
        if args.use_sudo:
            rc, out = ch.run(["sudo", b + "/install.sh", "--yes"], user=USER)
        else:
            rc, out = ch.run([b + "/install.sh", "--yes"], env={"SUDO_USER": USER})
        record("rerun: install.sh is idempotent", rc == 0 and "nothing to do" in out and "smoke tests: PASS" in out)

        # --- foreign venv protection ---------------------------------------------
        (home / "foreign-venv").mkdir(exist_ok=True)
        (home / "foreign-venv" / "keep.txt").write_text("user data\n")
        sh(["chroot", ch.root, "chown", "-R", "%s:%s" % (USER, USER), "/home/%s/foreign-venv" % USER])
        rc, out = ch.run([b + "/install.sh", "--skip-system", "--venv", "/home/%s/foreign-venv" % USER], user=USER)
        record("reject: existing non-bundle venv untouched", rc != 0 and "not created by this bundle" in out and
               (home / "foreign-venv/keep.txt").is_file())
        rc, out = ch.run([b + "/uninstall-user-env.sh", "--venv", "/home/%s/foreign-venv" % USER, "--yes"], user=USER)
        record("uninstall: refuses unmarked directory", rc != 0 and (home / "foreign-venv/keep.txt").is_file())

        # --- uninstall + skip-system rebuild --------------------------------------
        rc, out = ch.run([b + "/uninstall-user-env.sh", "--dry-run"], user=USER)
        record("uninstall: dry run removes nothing", rc == 0 and venv.is_dir())
        rc, out = ch.run([b + "/uninstall-user-env.sh", "--yes"], user=USER)
        rc2, out2 = ch.run(["dpkg-query", "-W", "-f", "${db:Status-Abbrev}", "python3"])
        record("uninstall: venv + launchers removed, system packages kept",
               rc == 0 and not venv.exists() and not (home / ".local/bin/madd-python").exists()
               and out2.startswith("ii"))
        rc, out = ch.run([b + "/install.sh", "--skip-system"], user=USER)
        record("install --skip-system as user (offline venv rebuild)", rc == 0 and "smoke tests: PASS" in out)
        if rc != 0:
            print(out[-3000:])
    finally:
        if args.gui:
            sh(["pkill", "-f", "Xvfb :99"], check=False)
        ch.umount()

    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print("\n%d tests, %d failed" % (len(RESULTS), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
