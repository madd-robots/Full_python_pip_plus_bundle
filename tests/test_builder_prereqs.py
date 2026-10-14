#!/usr/bin/env python3
"""Regression tests for the builder entry point and its prerequisite gates.

* ShellCheck is a mandatory release gate: a missing shellcheck stops the build
  before any work starts (never a SKIP), on every builder environment.
* Environment detection: Termux runs the APT stage through proot-distro and
  does not need APT itself; a Debian/Ubuntu host runs it locally.
* --target-arch must match the profile; --install-staging-prereqs installs
  missing tools (recorded by an apt-get/pkg stub, nothing is really installed).
* madd_stage.py's static checks record FAIL, not SKIP, without shellcheck.

Runs build_madd_python_bundle.sh --preflight-only with a controlled PATH of
symlinks to the host's real tools. Run on a Debian/Ubuntu host with shellcheck:
    python3 tests/test_builder_prereqs.py -v
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build_madd_python_bundle.sh"
STAGE = REPO / "builder" / "stage.sh"
sys.path.insert(0, str(REPO / "builder"))
import madd_stage  # noqa: E402

BASE_TOOLS = ["bash", "env", "dirname", "sed", "tr", "uname", "id", "cat", "head", "mkdir", "du",
              "python3", "tar", "gzip", "sha256sum", "find", "awk", "grep", "sort", "comm", "cmp", "df",
              "shellcheck"]
APT_TOOLS = ["apt-get", "apt-cache", "dpkg-deb", "gpgv"]
RECORDER = '#!/bin/sh\nprintf \'%%s\\n\' "%s $*" >>"$MADD_TEST_LOG"\n'
# Stand-in for proot-distro: records the call, applies --bind SRC:DST mappings to
# the command's arguments and runs it on this host with the system PATH (as if
# inside the Ubuntu environment).
FAKE_PD = """#!/usr/bin/env python3
import os, sys
with open(os.environ["MADD_TEST_LOG"], "a") as fh:
    fh.write("proot-distro " + " ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if not args or args[0] != "login":
    sys.exit(0)
binds = []
i = 2
while args[i] != "--":
    if args[i] == "--bind":
        src, dst = args[i + 1].split(":", 1)
        binds.append((dst, src))
        i += 2
    else:
        i += 1
cmd = []
for a in args[i + 1:]:
    for dst, src in binds:
        if a == dst or a.startswith(dst + "/"):
            a = src + a[len(dst):]
    cmd.append(a)
os.execvpe(cmd[0], cmd, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root"})
"""


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="madd-prereq-test-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.out = self.tmp / "out"
        self.log = self.tmp / "calls.log"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make_path(self, tools, stubs=(), extra=()):
        for tool in tools:
            if tool in stubs:
                stub = self.bin / tool
                stub.write_text(RECORDER % tool)
                stub.chmod(0o755)
                continue
            real = shutil.which(tool)
            if real is None:
                self.skipTest("host tool not available: %s" % tool)
            (self.bin / tool).symlink_to(real)
        for name, text in extra:
            f = self.bin / name
            f.write_text(text)
            f.chmod(0o755)

    def run_build(self, *extra, env_extra=None):
        env = {"PATH": str(self.bin), "HOME": str(self.tmp), "MADD_TEST_LOG": str(self.log)}
        env.update(env_extra or {})
        cmd = [str(self.bin / "bash"), str(BUILD), "--output", str(self.out), "--min-free-gb", "0",
               "--preflight-only", *extra]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        return proc, proc.stdout + proc.stderr

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    # -- ShellCheck gate ---------------------------------------------------
    def test_debian_host_shellcheck_present_preflight_passes(self):
        self.make_path(BASE_TOOLS + APT_TOOLS)
        proc, out = self.run_build()
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("builder environment:", out)
        self.assertIn("APT stage will run locally", out)
        self.assertIn("shellcheck 0.", out)

    def test_debian_host_shellcheck_missing_fails_early(self):
        self.make_path([t for t in BASE_TOOLS if t != "shellcheck"] + APT_TOOLS)
        proc, out = self.run_build()
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("missing builder prerequisites: shellcheck", out)
        self.assertIn("mandatory release gate", out)
        self.assertIn("apt-get install", out)
        self.assertNotIn("[madd-stage]", out)
        self.assertNotIn("preflight complete", out)

    def test_termux_shellcheck_missing_fails_with_pkg_hint(self):
        self.make_path([t for t in BASE_TOOLS if t != "shellcheck"], extra=[("proot-distro", FAKE_PD)])
        proc, out = self.run_build(env_extra={"TERMUX_VERSION": "0.118.3"})
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("missing builder prerequisites: shellcheck", out)
        self.assertIn("pkg install", out)

    @unittest.skipUnless(os.geteuid() == 0, "--install-staging-prereqs on a Debian host requires root")
    def test_install_prereqs_installs_missing_shellcheck(self):
        # apt-get is a recording stub: nothing is installed for real, so the
        # post-install re-check still reports shellcheck missing (and fails).
        self.make_path([t for t in BASE_TOOLS if t != "shellcheck"] + APT_TOOLS, stubs={"apt-get"})
        proc, out = self.run_build("--install-staging-prereqs")
        calls = self.calls()
        self.assertIn("apt-get update", calls, out)
        install = [c for c in calls if c.startswith("apt-get install")]
        self.assertTrue(install and "shellcheck" in install[0].split(), calls)
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("still missing after installation: shellcheck", out)

    @unittest.skipUnless(os.geteuid() == 0, "--install-staging-prereqs on a Debian host requires root")
    def test_install_prereqs_does_not_install_builder_tools_when_present(self):
        self.make_path(BASE_TOOLS + APT_TOOLS, stubs={"apt-get"})
        proc, out = self.run_build("--install-staging-prereqs")
        self.assertEqual(proc.returncode, 0, out)
        self.assertFalse([c for c in self.calls() if "shellcheck" in c.split()], self.calls())

    # -- environment detection and stage placement -----------------------------
    def test_termux_uses_proot_for_apt_stage_only(self):
        # No apt-get/dpkg-deb on the "Termux" side: APT is only needed inside proot.
        self.make_path(BASE_TOOLS, extra=[("proot-distro", FAKE_PD)])
        proc, out = self.run_build(env_extra={"TERMUX_VERSION": "0.118.3", "PREFIX": "/data/data/com.termux/files/usr"})
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("builder environment: Termux 0.118.3 on Android", out)
        self.assertIn("APT stage will run in proot-distro environment: ubuntu-claude-secure", out)
        pd = [c for c in self.calls() if c.startswith("proot-distro login ubuntu-claude-secure")]
        self.assertEqual(len(pd), 2, self.calls())  # usability probe + APT-stage prerequisite check
        self.assertIn("stage.sh --profile mx25-amd64 --check-prereqs-only", pd[1])

    def test_termux_custom_distro_name(self):
        self.make_path(BASE_TOOLS, extra=[("proot-distro", FAKE_PD)])
        proc, out = self.run_build("--distro", "my-ubuntu", env_extra={"TERMUX_VERSION": "1"})
        self.assertEqual(proc.returncode, 0, out)
        self.assertTrue(all(" my-ubuntu " in c + " " for c in self.calls() if c.startswith("proot-distro login")))

    def test_target_arch_must_match_profile(self):
        self.make_path(BASE_TOOLS + APT_TOOLS)
        proc, out = self.run_build("--target-arch", "aarch64")
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("targets amd64, not arm64", out)
        proc, out = self.run_build("--target-arch", "x86_64")
        self.assertEqual(proc.returncode, 0, out)

    def test_ubuntu_profile_selectable(self):
        self.make_path(BASE_TOOLS + APT_TOOLS)
        proc, out = self.run_build("--profile", "ubuntu2404-amd64")
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("Ubuntu 24.04 LTS (noble)", out)

    # -- APT-stage entry (stage.sh) ----------------------------------------------
    def run_stage(self, *extra):
        env = {"PATH": str(self.bin), "HOME": str(self.tmp)}
        return subprocess.run([str(self.bin / "bash"), str(STAGE), "--profile", "mx25-amd64",
                               "--check-prereqs-only", *extra], env=env, capture_output=True, text=True)

    def test_stage_reports_missing_gpgv(self):
        self.make_path([t for t in BASE_TOOLS + APT_TOOLS if t != "gpgv"])
        proc = self.run_stage()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing APT-stage prerequisites: gpgv", proc.stdout + proc.stderr)

    def test_stage_does_not_need_shellcheck(self):
        # Static checks run on the builder side, not in the APT-stage environment.
        self.make_path([t for t in BASE_TOOLS + APT_TOOLS if t != "shellcheck"])
        proc = self.run_stage()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class StaticChecksShellcheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="madd-static-test-"))
        args = argparse.Namespace(output=str(self.tmp / "out"), profile="mx25-amd64", min_free_gb="0",
                                  target_state="", skip_baseline_model=False)
        self.builder = madd_stage.Builder(args)
        self.builder.bundle_dir = self.tmp / "bundle"
        self.builder.bundle_dir.mkdir()
        (self.builder.bundle_dir / "ok.sh").write_text('#!/usr/bin/env bash\nset -eu\nprintf "%s\\n" ok\n')
        (self.builder.bundle_dir / "ok.py").write_text("print('ok')\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_missing_shellcheck_fails_not_skip(self):
        real_which = shutil.which
        with mock.patch.object(madd_stage.shutil, "which",
                               side_effect=lambda n, *a, **k: None if n == "shellcheck" else real_which(n, *a, **k)):
            with self.assertRaises(madd_stage.BuildError):
                self.builder.static_checks()
        statuses = {name: status for name, status, _d in self.builder.results}
        self.assertEqual(statuses.get("static: shellcheck (warning level)"), "FAIL")
        self.assertNotIn("SKIP", statuses.values())

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed on this host")
    def test_present_shellcheck_runs_and_passes(self):
        self.builder.static_checks()
        statuses = {name: status for name, status, _d in self.builder.results}
        self.assertEqual(statuses.get("static: shellcheck (warning level)"), "PASS")
        self.assertNotIn("SKIP", statuses.values())


if __name__ == "__main__":
    unittest.main()
