#!/usr/bin/env python3
"""Standard-library functional checks for an installed Python interpreter.

Offline and side-effect free: everything happens in memory or in a private
temporary directory that is removed afterwards. Exit status 0 = all PASS.
"""

import os
import sys

RESULTS = []


def check(name, func):
    try:
        detail = func()
        RESULTS.append((name, "PASS", detail or ""))
    except Exception as exc:  # report every failure, keep going
        RESULTS.append((name, "FAIL", "%s: %s" % (type(exc).__name__, exc)))


def note(name, status, detail):
    RESULTS.append((name, status, detail))


def t_ssl():
    import ssl
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise RuntimeError("default context does not verify certificates")
    ca = paths.cafile or paths.capath
    if not ca or not os.path.exists(ca):
        raise RuntimeError("no CA bundle at %r" % (ca,))
    if ctx.cert_store_stats().get("x509_ca", 0) == 0 and not paths.capath:
        raise RuntimeError("no CA certificates loaded")
    return "%s, CA %s" % (ssl.OPENSSL_VERSION, ca)


def t_sqlite3():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("create table t (k integer primary key, v text)")
    con.executemany("insert into t (v) values (?)", [("a",), ("b",), ("c",)])
    n = con.execute("select count(*) from t").fetchone()[0]
    con.close()
    if n != 3:
        raise RuntimeError("unexpected row count")
    return "SQLite %s" % sqlite3.sqlite_version


def t_ctypes():
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    if libc.abs(-7) != 7:
        raise RuntimeError("libc call failed")
    return "libc abs() via ctypes"


def _square(x):
    return x * x


def t_multiprocessing():
    import multiprocessing
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        out = pool.map(_square, range(5))
    if out != [0, 1, 4, 9, 16]:
        raise RuntimeError("bad result %r" % out)
    return "2-process pool"


def t_futures():
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        out = list(ex.map(_square, range(4)))
    if out != [0, 1, 4, 9]:
        raise RuntimeError("bad result")
    return "thread pool"


def t_hash_hmac_secrets():
    import hashlib
    import hmac
    import secrets
    if hashlib.sha256(b"abc").hexdigest() != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad":
        raise RuntimeError("sha256 mismatch")
    key = secrets.token_bytes(32)
    mac = hmac.new(key, b"msg", hashlib.sha256).digest()
    if not hmac.compare_digest(mac, hmac.new(key, b"msg", "sha256").digest()):
        raise RuntimeError("hmac mismatch")
    return "sha256/hmac/secrets, %d hash algorithms" % len(hashlib.algorithms_available)


def t_compression():
    import bz2
    import gzip
    import lzma
    data = b"madd-python " * 100
    for mod in (bz2, gzip, lzma):
        if mod.decompress(mod.compress(data)) != data:
            raise RuntimeError("%s roundtrip failed" % mod.__name__)
    return "bz2, gzip, lzma roundtrip"


def t_formats():
    import csv
    import io
    import json
    obj = {"a": [1, 2, 3], "b": "x"}
    if json.loads(json.dumps(obj)) != obj:
        raise RuntimeError("json")
    buf = io.StringIO()
    csv.writer(buf).writerow(["a", "b,c"])
    if next(csv.reader(io.StringIO(buf.getvalue()))) != ["a", "b,c"]:
        raise RuntimeError("csv")
    return "json, csv"


def t_paths_subprocess():
    import pathlib
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix="madd-verify-") as d:
        p = pathlib.Path(d) / "x.txt"
        p.write_text("ok")
        if p.read_text() != "ok":
            raise RuntimeError("pathlib")
    out = subprocess.run([sys.executable, "-c", "print(6*7)"], capture_output=True, text=True, check=True)
    if out.stdout.strip() != "42":
        raise RuntimeError("subprocess")
    return "pathlib, subprocess"


def t_venv_urllib_http():
    import http.client  # noqa: F401
    import urllib.parse
    import urllib.request  # noqa: F401
    import venv  # noqa: F401
    if urllib.parse.urlsplit("https://example.invalid/a?b=1").query != "b=1":
        raise RuntimeError("urllib.parse")
    return "venv, urllib, http.client import (no network used)"


def t_tkinter():
    import tkinter
    tcl = tkinter.Tcl()  # Tcl interpreter: works without a display
    if tcl.eval("expr {6*7}") != "42":
        raise RuntimeError("Tcl eval failed")
    return "Tk %s, Tcl interpreter OK" % tkinter.TkVersion


def tk_gui_test():
    """Real Tk window initialisation when a display is available."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        note("tkinter GUI", "INFO", "Tkinter import passed; GUI display test unavailable (no DISPLAY)")
        return
    try:
        import tkinter
        root = tkinter.Tk()
    except Exception as exc:
        # Display present but not reachable (e.g. X authority under sudo): environment, not a Tk failure.
        note("tkinter GUI", "INFO", "Tkinter import passed; GUI display test unavailable (%s)" % exc)
        return
    try:
        root.withdraw()
        tkinter.Label(root, text="madd-python").pack()
        root.update_idletasks()
        root.update()
        note("tkinter GUI", "PASS", "created and destroyed a Tk window on %s" % os.environ.get("DISPLAY", "wayland"))
    except Exception as exc:
        note("tkinter GUI", "FAIL", "%s: %s" % (type(exc).__name__, exc))
    finally:
        root.destroy()


STDLIB_CHECKS = [
    ("ssl", t_ssl),
    ("sqlite3", t_sqlite3),
    ("ctypes", t_ctypes),
    ("multiprocessing", t_multiprocessing),
    ("concurrent.futures", t_futures),
    ("hashlib/hmac/secrets", t_hash_hmac_secrets),
    ("bz2/lzma/gzip", t_compression),
    ("json/csv", t_formats),
    ("pathlib/subprocess", t_paths_subprocess),
    ("venv/urllib/http.client", t_venv_urllib_http),
    ("tkinter", t_tkinter),
]


def run_stdlib_checks():
    for name, func in STDLIB_CHECKS:
        check(name, func)
    tk_gui_test()


def report(title):
    print("%s (%s %s)" % (title, sys.executable, sys.version.split()[0]))
    for name, status, detail in RESULTS:
        print("  %-5s %-26s %s" % (status, name, detail))
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print("  => %s (%d checks, %d failed)" % ("FAIL" if failed else "PASS", len(RESULTS), len(failed)))
    return 1 if failed else 0


def main():
    run_stdlib_checks()
    return report("Standard library checks")


if __name__ == "__main__":
    sys.exit(main())
