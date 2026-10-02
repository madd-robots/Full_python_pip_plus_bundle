# Full_python_pip_plus_bundle

Offline Python development bundle builder for MX Linux. It builds one archive,
`madd-python-full-mx25-amd64.tar.gz`. You copy that archive to an air-gapped
MX Linux 25 (Debian 13 "trixie") amd64 machine, verify it, and install it with
no network access. The archive contains:

* Python 3.13 with pip, venv, Tkinter, and -dev headers
* the native build toolchain
* a complete hash-pinned wheelhouse: NumPy, SciPy, pandas, matplotlib,
  cryptography, requests, pytest, and every dependency
* manifests, checksums, a verifier, an installer, and smoke tests

No Bluetooth, Wi-Fi, cellular, or other radio software is bundled. Every build
audits for this.

## How it fits together

    Android phone (Termux)                        Air-gapped MX 25 laptop (amd64)
    ----------------------                        -------------------------------
    build_madd_python_bundle.sh                   ./verify.sh
      └─ proot-distro ubuntu-claude-secure        sudo ./install.sh
           └─ builder/stage.sh                      ├─ preflight (no changes)
                └─ builder/madd_stage.py            ├─ APT from bundled file: repo only
                     ├─ isolated APT (amd64)        ├─ ~/.venvs/madd-python (pip --no-index)
                     ├─ pip download (x86_64 tags)  └─ smoke tests
                     ├─ audits + offline tests
                     └─ output/dist/*.tar.gz  ──USB──▶

* Termux is never the build target. All staging work runs inside the existing
  `ubuntu-claude-secure` proot-distro environment. Both the 4.x layout
  (`installed-rootfs/`) and the 5.x layout (`containers/`) are detected.
* The phone is ARM64. Target artifacts are always requested explicitly:
  Debian packages via an isolated APT instance with `APT::Architecture
  "amd64"`, and wheels via `pip download --platform manylinux_*_x86_64
  --python-version 3.13 --abi cp313`. Every wheel's tags and every .deb's
  architecture are checked afterwards.
* The Python version, ABI, and glibc baseline are read from the resolved
  Debian packages (python3, libc6). They are not hard-coded.
* Nothing in the build needs root. Root inside proot is only used if you ask
  for `--install-staging-prereqs`.

## Building (on the phone)

    # once, in Termux:
    pkg install proot-distro
    # this repository inside Termux's home, then:
    ./build_madd_python_bundle.sh --install-staging-prereqs     # first run
    ./build_madd_python_bundle.sh                               # later runs

`--install-staging-prereqs` runs `apt-get update` and installs python3,
python3-venv, gpgv, ca-certificates, debian-archive-keyring, and shellcheck
*inside ubuntu-claude-secure only*. Without it the builder lists exactly what
is missing.

Needs about 4 GiB free (checked; change it with `--min-free-gb`). Downloads
are cached in `output/cache/`, so reruns are fast. Every run builds in a fresh
`output/staging.*` directory. The previous archive in `output/dist/` is
replaced only after the new one has passed every build-time test.

On a Debian/Ubuntu Linux machine you can build without Termux:
`./build_madd_python_bundle.sh --staging local`.

### Matching the laptop exactly (recommended)

An air-gapped laptop may be missing months of updates. Some bundled packages
need newer versions of base libraries that are already installed, for example
the util-linux family or libc. By default the builder covers this by modelling
an un-updated MX 25 / Debian 13.0 machine from the signed Debian 13.0 release
index (snapshot.debian.org). It adds the base packages that machine would have
to upgrade in lockstep.

For an exact match, export the laptop's package state and build against it:

    # on the MX laptop (no root, no network, read-only):
    ./export-target-state.sh /media/usb/mx-target-state
    # on the phone:
    ./build_madd_python_bundle.sh --target-state /path/to/mx-target-state

`export-target-state.sh` is included in every bundle and in
`bundle_template/`. It copies `/var/lib/dpkg/status`, os-release, the
architecture, and the MX version. No user files are copied.

### Outputs (`output/dist/`)

| File | Purpose |
|------|---------|
| madd-python-full-mx25-amd64.tar.gz | the bundle |
| madd-python-full-mx25-amd64.tar.gz.sha256 | archive checksum; copy it with the archive |
| madd-python-full-mx25-amd64.build-report.txt | every build test with PASS/FAIL, plus BUILD_INFO |
| madd-python-full-mx25-amd64.wireless-audit.txt | what the exclusion audit checked and found |

## Installing (on the MX laptop)

    sha256sum -c madd-python-full-mx25-amd64.tar.gz.sha256
    tar -xzf madd-python-full-mx25-amd64.tar.gz
    cd madd-python-full-mx25-amd64
    ./verify.sh
    sudo ./install.sh

The bundle's own README.md covers the details: options, what is and is not
changed, removal, and troubleshooting.

## What the build checks

Every build records these in the build report and stops on any failure:

* Every .deb matches the SHA-256 and size in Debian's signed index (APT
  verifies the InRelease signatures; nothing disables that).
* Every .deb is amd64 or all. Every wheel's tags fit CPython 3.13 / cp313 /
  manylinux x86_64.
* Wheel dependency closure is evaluated with the target's environment markers
  (python_version 3.13, platform_machine x86_64, ...). It does not use the
  phone's own markers.
* Wireless / Bluetooth / cellular audit. It checks Debian package names,
  relationship fields, descriptions, every file path inside every .deb, Python
  project names, all Requires-Dist entries (including optional extras),
  summaries and classifiers, and top-level modules. Reviewed exceptions live in
  `builder/wireless-allowlist.txt`, each with a written reason, and each one
  is listed individually in the report. Currently there is one: kernel UAPI C
  headers in linux-libc-dev, such as `rfkill.h` and `nl80211.h`. libc6-dev
  requires them, and they are header files only.
* Static checks: `bash -n`, zero backticks, shellcheck, Python syntax, and no
  `shell=True`.
* Offline resolution from the bundled local repository alone: for an empty
  system, for every bundled package, for both un-updated-machine models, and
  for `--target-state` when given. Offline hash-checked resolution of
  requirements.lock.
* Clean extraction of the final tar.gz. verify.sh must pass, must reject a
  modified file, and must reject a missing file.

## Repository layout

    build_madd_python_bundle.sh   entry point (Termux -> proot, or --staging local)
    builder/
      stage.sh                    runs inside the staging environment
      madd_stage.py               resolution, download, audits, manifests, tests, archive
      profiles/mx25-amd64.conf    target profile (key=value; parsed, never sourced)
      debian-packages.txt         requested Debian packages
      requirements.in             requested Python packages
      sdist-fallback.txt          packages to bundle as source (empty: all have wheels)
      wireless-allowlist.txt      reviewed audit exceptions with reasons
      tools-requirements.txt      hash-pinned staging tools (pip, packaging)
    bundle_template/              files copied into every bundle
      install.sh verify.sh uninstall-user-env.sh export-target-state.sh README.md
      scripts/smoke_test.py scripts/verify_python.py
    tests/
      test_install_chroot.py      installer tests in a disposable Debian 13 chroot
      fake-proot-distro           proot-distro stand-in for testing the Termux path

Another target (for example arm64) would need a new profile in
`builder/profiles/`. The builder takes the Debian architecture and wheel
platform from the profile.

## Testing the installer

`tests/test_install_chroot.py` runs as root on a Linux test host, against a
chroot made with mmdebstrap. It runs the installer inside `unshare -n`, a
network namespace with no network at all. It covers:

* verify and preflight
* rejection of a corrupted file, the wrong architecture, a missing dependency,
  the wrong distribution, and a foreign venv
* a real installation and the smoke tests
* launchers and PEP 668
* rerun idempotency, uninstall, and a `--skip-system` rebuild

See the script header for setup.
