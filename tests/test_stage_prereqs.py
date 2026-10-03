#!/usr/bin/env python3
"""Regression tests: ShellCheck is a required staging prerequisite.

Runs builder/stage.sh with a controlled PATH made of symlinks to the host's
real tools (optionally without shellcheck, optionally with a recording
apt-get stub), ending in madd_stage.py --check-storage-only so no network or
package changes happen. Also checks that madd_stage.py's static checks fail,
rather than SKIP, when shellcheck is absent.

Run on a Debian/Ubuntu host that has shellcheck installed:
    python3 tests/test_stage_prereqs.py -v
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
STAGE = REPO / "builder" / "stage.sh"
sys.path.insert(0, str(REPO / "builder"))
import madd_stage  # noqa: E402

# Everything stage.sh calls before exec'ing madd_stage.py.
TOOLS = ["bash", "env", "dirname", "sed", "tr", "uname", "id", "python3", "apt-get", "dpkg-deb",
         "tar", "gzip", "sha256sum", "find", "gpgv", "shellcheck"]


class StagePrereqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="madd-prereq-test-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.out = self.tmp / "out"
        self.apt_log = self.tmp / "apt-get.log"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make_path(self, omit=(), apt_stub=False):
        for tool in TOOLS:
            if tool in omit:
                continue
            if tool == "apt-get" and apt_stub:
                stub = self.bin / "apt-get"
                stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$MADD_TEST_APT_LOG"\n')
                stub.chmod(0o755)
                continue
            real = shutil.which(tool)
            if real is None:
                self.skipTest("host tool not available: %s" % tool)
            (self.bin / tool).symlink_to(real)

    def run_stage(self, *extra):
        env = {"PATH": str(self.bin), "HOME": str(self.tmp), "MADD_TEST_APT_LOG": str(self.apt_log)}
        cmd = [str(self.bin / "bash"), str(STAGE), "--output", str(self.out),
               "--min-free-gb", "0", "--check-storage-only", *extra]
        return subprocess.run(cmd, env=env, capture_output=True, text=True)

    def test_shellcheck_present_ordinary_build_proceeds(self):
        self.make_path()
        proc = self.run_stage()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("missing staging prerequisites", proc.stdout + proc.stderr)

    def test_shellcheck_missing_ordinary_build_fails(self):
        self.make_path(omit={"shellcheck"})
        proc = self.run_stage()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("missing staging prerequisites: shellcheck", out)
        self.assertIn("apt-get install --no-install-recommends", out)
        self.assertIn("--install-staging-prereqs", out)
        # Stopped in stage.sh: madd_stage.py never ran.
        self.assertNotIn("[madd-stage]", out)

    def test_other_missing_prereq_still_reported(self):
        self.make_path(omit={"gpgv"})
        proc = self.run_stage()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("missing staging prerequisites: gpgv", out)

    @unittest.skipUnless(os.geteuid() == 0, "--install-staging-prereqs path requires root")
    def test_install_prereqs_installs_missing_shellcheck(self):
        self.make_path(omit={"shellcheck"}, apt_stub=True)
        proc = self.run_stage("--install-staging-prereqs")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.apt_log.read_text().splitlines()
        self.assertEqual(calls[0], "update")
        self.assertTrue(calls[1].startswith("install -y --no-install-recommends "), calls)
        self.assertIn("shellcheck", calls[1].split())

    @unittest.skipUnless(os.geteuid() == 0, "--install-staging-prereqs path requires root")
    def test_install_prereqs_noop_when_nothing_missing(self):
        self.make_path(apt_stub=True)
        proc = self.run_stage("--install-staging-prereqs")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.apt_log.exists(), "apt-get must not run when nothing is missing")


class StaticChecksShellcheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="madd-static-test-"))
        args = argparse.Namespace(output=str(self.tmp / "out"), profile="mx25-amd64", min_free_gb="0",
                                  debian_keyring="", keep_staging=False, target_state="",
                                  skip_baseline_model=False)
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
