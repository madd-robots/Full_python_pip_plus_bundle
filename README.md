# Full_python_pip_plus_bundle

This builds an offline Python development bundle from Termux on an Android
phone. The bundle is installed on an air-gapped x86_64 laptop running
MX Linux 25 (or Debian 13), or Ubuntu 24.04. The phone has Internet access;
the laptop needs none.

Each bundle contains:

* CPython with pip, venv, Tkinter (Tcl/Tk), and -dev headers
* the native build toolchain
* every Debian/Ubuntu package these need, as a signed local repository
* a complete hash-pinned wheelhouse: NumPy, SciPy, pandas, matplotlib, Pillow,
  cryptography, PyNaCl, bcrypt, paramiko, requests, PyYAML, rich, pytest, and
  every dependency
* manifests, an inventory, a verifier, an installer, and smoke tests

No Bluetooth, Wi-Fi, cellular, or other radio software is bundled. Every build
audits for this.

## Quick start (phone)

    # once, in Termux:
    pkg install python shellcheck proot-distro
    # in the project directory:
    ./build_madd_python_bundle.sh                      # MX Linux 25 / Debian 13 bundle
    ./build_madd_python_bundle.sh --profile ubuntu2404-amd64   # Ubuntu 24.04 bundle

The first run may need `--install-staging-prereqs`. In Termux it installs
missing builder tools with `pkg`. Inside `ubuntu-claude-secure` it installs
python3, gpgv, ca-certificates, and the archive keyring with `apt-get`.
`--preflight-only` checks everything and builds nothing.

The result is in `output/dist/`. Copy the `.tar.gz` and its `.sha256` to the
laptop, then:

    sha256sum -c madd-python-full-mx25-amd64.tar.gz.sha256
    tar -xzf madd-python-full-mx25-amd64.tar.gz
    cd madd-python-full-mx25-amd64
    ./verify.sh
    sudo ./install.sh

## Where each stage runs

    Termux (native, preferred)                     ubuntu-claude-secure (proot)
    -------------------------------------------    ------------------------------
    build_madd_python_bundle.sh
      detect environment + builder/target arch
      prerequisite gates (ShellCheck mandatory)
      builder/madd_stage.py
        private tools venv (hash-pinned pip)
        APT stage ───── proot-distro login ──────▶  builder/stage.sh
                                                     builder/madd_debstage.py
                                                       resolve + download .debs
                                                       (APT::Architecture amd64)
        ◀──── signed repo slice + JSON ───────────     offline APT self-tests
        re-verify every hand-off file
        target wheelhouse (pip download with explicit
          --platform manylinux_*_x86_64 --abi cp3XX)
        dependency closure with target markers
        manifests, inventory, wireless audit
        ShellCheck, offline pip test, SHA256SUMS
        tar.gz + extraction/tamper self-tests
      output/dist/<bundle>.tar.gz (+ .sha256)

* Only the Debian/Ubuntu package stage uses Ubuntu. It needs APT to resolve
  dependencies and to verify the archive signatures.
* Downloading wheels, hashing, building manifests, auditing, ShellCheck, and
  archiving all run directly in Termux.
* On a Debian/Ubuntu host, or inside the proot environment itself, every
  stage runs locally.
* Builder architecture (for example the phone's aarch64) and target
  architecture (amd64/x86_64) are kept separate. Every artifact is requested
  explicitly for the target, and every .deb architecture and wheel tag is
  checked afterwards. Nothing built for the target is executed on the builder.
  Both architectures are recorded in BUILD_INFO.txt and inventory.txt.
* The proot-distro 4.x and 5.x layouts are both supported. The default
  environment name is `ubuntu-claude-secure` (`--distro NAME` to change).

## Trust chain

* Debian/Ubuntu packages are resolved by an isolated APT instance from the
  official HTTPS mirrors. APT verifies the InRelease signatures with the
  archive keyring, and every .deb is matched against the SHA-256 in that
  signed index.
* The bundle ships the signed InRelease and Packages.xz files unchanged.
  Packages.xz is checked against InRelease. Only the bundled .deb files are
  included.
* On the laptop, APT verifies those signatures with the laptop's own
  installed archive keyring. There is no `trusted=yes`. Pins and a check of
  APT's plan restrict installation to exactly the bundled versions.
* Wheels come from PyPI over HTTPS. They are locked with SHA-256
  (`requirements.lock`) and installed with
  `pip install --no-index --require-hashes`. pip cannot fall back to the
  network: `--no-index` is passed, pip configuration and environment are
  ignored (`--isolated`, `env -i`), and proxies point to an unreachable
  address.
* The builder's own tools (pip, packaging) are hash-pinned in
  `builder/tools-requirements.txt`.
* Exception: the archive's Valid-Until date (a freshness check, normally 7
  days for the -updates and -security suites) is not enforced for the bundled
  local sources. An offline snapshot is always used after that date.
  Signature and hash checks stay on.

## Un-updated laptops

An air-gapped laptop may be missing months of updates. Some bundled packages
then need newer versions of installed base libraries, such as libc or the
util-linux family. The builder models an un-updated machine from the signed
release-time index. For Debian 13.0 that is snapshot.debian.org; for Ubuntu it
is the frozen `noble` pocket. It bundles exactly the base packages such a
machine would have to upgrade.

For an exact match, run `./export-target-state.sh DIR` on the laptop (no root,
no network, read-only; included in every bundle). Carry DIR to the phone and
build with `--target-state DIR`.

## Outputs (`output/dist/`)

| File | Purpose |
|------|---------|
| `<bundle>.tar.gz` | the bundle |
| `<bundle>.tar.gz.sha256` | archive checksum; copy it with the archive |
| `<bundle>.build-report.txt` | every build test with PASS/FAIL, plus BUILD_INFO |
| `<bundle>.inventory.txt` | target/builder facts and every package with its SHA-256 |
| `<bundle>.wireless-audit.txt` | what the exclusion audit checked and found |

A previous bundle is replaced only after the new one has passed every
build-time test. Every mandatory test must PASS; any FAIL or SKIP stops the
release. Downloads are cached in `output/cache/`.

## Repository layout

    build_madd_python_bundle.sh   the entry point (Termux, Ubuntu proot, or Debian/Ubuntu host)
    builder/
      madd_stage.py               orchestrator (runs natively in Termux)
      madd_debstage.py            APT stage (stdlib only; local or inside proot)
      madd_common.py              shared helpers
      stage.sh                    APT-stage entry and prerequisite check
      profiles/*.conf             targets: mx25-amd64, ubuntu2404-amd64
      debian-packages.txt         requested system packages
      requirements.in             requested Python packages
      sdist-fallback.txt          packages to bundle as source (empty: all have wheels)
      wireless-allowlist.txt      reviewed audit exceptions with reasons
      tools-requirements.txt      hash-pinned builder tools
    bundle_template/              files copied into every bundle
      install.sh verify.sh uninstall-user-env.sh export-target-state.sh README.md
      scripts/smoke_test.py scripts/verify_python.py
    tests/
      test_builder_prereqs.py     entry point, environment detection, ShellCheck gate
      test_install_chroot.py      offline install tests in a disposable chroot
      fake-proot-distro           proot-distro stand-in for testing the Termux path

## Testing

* `python3 tests/test_builder_prereqs.py -v` covers:
  * environment detection and where each stage runs;
  * the ShellCheck gate (missing ShellCheck stops the build; it is never
    skipped);
  * `--target-arch` checks;
  * `--install-staging-prereqs`.
* `tests/test_install_chroot.py --rootfs DIR --tarball FILE [--use-sudo] [--gui] [--foreign-tarball FILE]`
  runs as root on a Linux test host, against a disposable chroot made with
  mmdebstrap. The installer runs in `unshare -n` (no network at all). It
  covers:
  * verify and preflight;
  * rejection of a corrupted file, a missing .deb, a missing mandatory wheel,
    the wrong architecture, the wrong distribution, and a bundle for another
    distribution;
  * pip network fallback being impossible;
  * a real installation, Tkinter (including a real Tk window under Xvfb), and
    the import/functional smoke tests;
  * launchers, PEP 668, rerun, recovery, and uninstall.
