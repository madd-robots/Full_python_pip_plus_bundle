# madd-python-full offline bundle (MX Linux 25 / Debian 13, amd64)

A self-contained, offline Python development toolchain for an air-gapped
MX Linux 25.x (Debian 13 "trixie") amd64 machine. Nothing is downloaded at
install time.

## What is inside

| Item | Content |
|------|---------|
| payload/debs/ | Debian 13 amd64/all .deb files with a local APT repository index (Packages, Packages.gz, Release): python3, python3-full, -dev, -venv, -pip, -tk, setuptools, wheel, build-essential, gcc/g++/make/pkgconf, SSL/FFI/zlib/bz2/lzma/sqlite/readline/ncurses/gdbm/Tcl/Tk headers, ca-certificates, and their full dependency closure |
| payload/wheels/ | Complete hash-pinned wheelhouse (pip, numpy, scipy, pandas, matplotlib, cryptography, requests, pytest, ... and every dependency) for CPython 3.13 linux x86_64 |
| payload/sdists/ | Source fallbacks (empty unless BUILD_INFO.txt SDIST_COUNT > 0) |
| requirements.in / requirements.lock | Requested packages / frozen versions with SHA-256 |
| deb-manifest.txt / wheel-manifest.txt | Every artifact: name, version, architecture/tags, filename, SHA-256 |
| BUILD_INFO.txt | Target profile, Python version/ABI, glibc, counts, build date |
| wireless-audit.txt | What was checked to exclude Bluetooth/Wi-Fi/cellular packages, and the result |
| SHA256SUMS / MANIFEST.txt | Checksums and inventory of every file |
| export-target-state.sh | Captures this machine's package state for an exact-match rebuild |

## Install

1. On the machine that has the archive, check the archive against the
   separately copied checksum file:

        sha256sum -c madd-python-full-mx25-amd64.tar.gz.sha256

2. On the MX machine:

        tar -xzf madd-python-full-mx25-amd64.tar.gz
        cd madd-python-full-mx25-amd64
        ./verify.sh
        sudo ./install.sh

   To keep a log: `sudo ./install.sh 2>&1 | tee install.log`

install.sh first runs a full preflight (OS, Debian version, architecture,
disk space, bundle integrity, local repository consistency, and an APT
dependency-resolution simulation). It changes nothing unless every check
passes, and asks for confirmation before installing packages (`--yes` skips the
question). `./install.sh --preflight-only` runs the checks alone, without root.

### What install.sh does

* Copies the package repository into a private root-owned directory under
  /var/tmp, re-verifies its hashes, and points a temporary APT configuration
  (passed with `apt-get -c`) at that `file:` repository only. Your
  /etc/apt/sources.list, sources.list.d, pins, and MX repository settings are
  not read or modified, and no network source is configured.
* Installs only the packages in deb-install-list.txt and the dependencies they
  require. It never runs upgrade/full-upgrade/dist-upgrade and aborts if any
  package would be removed. Bundled versions are installed only where needed.
  Already-installed newer versions are kept. On a machine that has not
  installed recent updates, some already-installed base packages must be
  upgraded together with the bundle's versions, for example libc, OpenSSL,
  python3.13, and the util-linux family. The preflight lists every such
  upgrade before asking for confirmation. BUILD_INFO.txt (DEB_LOCKSTEP_BASE)
  names the base packages carried for this purpose.
* Creates `~/.venvs/madd-python` with `python3 -m venv` and installs the
  wheelhouse with `pip install --no-index --require-hashes`. Debian's
  externally-managed system Python is not modified (no
  `--break-system-packages`).
* Creates `~/.local/bin/madd-python`, `madd-pip`, and `madd-python-env`. It
  does not edit .bashrc or .profile; if ~/.local/bin is not on your PATH it
  prints the optional line to add.
* Runs smoke tests: system python3 standard-library checks, then imports and
  functional tests (NumPy, SciPy, pandas, matplotlib, cryptography round trips,
  SQLite, SSL/CA paths, pyserial loopback). Without a graphical session the
  Tkinter check reports "Tkinter import passed; GUI display test unavailable",
  which is not a failure. `sudo` usually drops DISPLAY. To run the GUI test
  afterwards from your desktop session, run
  `madd-python -B scripts/smoke_test.py` in this directory (`-B` keeps the
  bundle free of bytecode files, so verify.sh still passes).

It does not add services, daemons, cron jobs, hooks, firewall changes,
boot changes, or wireless/NetworkManager changes. Re-running it is safe: already
installed packages are left alone and the environment is updated in place.

### Options

    --preflight-only   checks only, no changes
    --yes              no confirmation prompt
    --skip-system      only (re)build the user environment; run as that user
    --target-user U    owner of the environment (default: the sudo user)
    --venv DIR         environment location
    --no-launchers     do not create the ~/.local/bin launchers

## Using it

    madd-python script.py          # environment's interpreter
    madd-pip list                  # environment's pip (offline: add --no-index)
    madd-python-env                # shell with the environment activated
    source ~/.venvs/madd-python/bin/activate

To add more packages later on an offline machine, copy their wheels and use
`madd-pip install --no-index --find-links DIR package`.

## Removing

    ./uninstall-user-env.sh --dry-run
    ./uninstall-user-env.sh

This removes only the environment and the launchers that this bundle created
(identified by marker files). Debian system packages are deliberately not
removed automatically because other software may depend on them. Review any
manual removal with `apt-get --simulate remove PACKAGE` first.

## Building a bundle that matches this machine exactly

`./export-target-state.sh DIR` (no root, no network, read-only) saves this
machine's package list, os-release, and architecture to DIR. Carry DIR to the
build device and run `build_madd_python_bundle.sh --target-state DIR`. The
builder then resolves against this exact machine.

## If install.sh stops

* Hash mismatch / missing / unexpected file: the copy is damaged or altered.
  Copy the archive again and re-check its .sha256.
* Architecture or distribution mismatch: this bundle is only for MX 25 /
  Debian 13 on amd64.
* Dependency resolution failed: the machine has newer versions of some core
  packages than the bundle (for example after installing later updates), or
  is missing something the bundle cannot provide. Nothing was changed. Run
  ./export-target-state.sh and rebuild the bundle with --target-state.
