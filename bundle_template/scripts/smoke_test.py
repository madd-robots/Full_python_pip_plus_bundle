#!/usr/bin/env python3
"""Smoke tests for the madd-python environment (run with the venv's python).

Offline: no network requests are made. Test keys and data exist only in memory.
Exit status 0 = all PASS.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_python as vp  # noqa: E402

IMPORTS = [
    "tkinter", "ssl", "sqlite3", "ctypes", "hashlib", "hmac", "secrets",
    "numpy", "scipy", "pandas", "matplotlib", "PIL", "cryptography", "OpenSSL",
    "bcrypt", "nacl", "Crypto", "requests", "httpx", "yaml", "psutil", "rich",
    "pytest", "serial",
    # remaining top-level packages from requirements.in
    "pip", "setuptools", "wheel", "virtualenv", "packaging", "build", "piptools",
    "setuptools_scm", "platformdirs", "filelock", "cffi", "pycparser", "argon2",
    "urllib3", "certifi", "charset_normalizer", "idna", "tomlkit", "jsonschema",
    "jinja2", "dateutil", "typing_extensions", "dotenv", "typer", "click", "tqdm",
    "watchdog", "pytest_cov", "black", "mypy",
]


def t_imports():
    failed = []
    for mod in IMPORTS:
        try:
            importlib.import_module(mod)
        except Exception as exc:
            failed.append("%s (%s)" % (mod, exc))
    if failed:
        raise RuntimeError("import failed: " + ", ".join(failed))
    return "%d modules" % len(IMPORTS)


def t_numpy_scipy_pandas():
    import numpy as np
    import pandas as pd
    from scipy import linalg
    a = np.array([[4.0, 1.0], [2.0, 3.0]])
    x = linalg.solve(a, np.array([1.0, 2.0]))
    if not np.allclose(a @ x, [1.0, 2.0]):
        raise RuntimeError("linear solve mismatch")
    df = pd.DataFrame({"v": np.arange(10)})
    if int(df["v"].sum()) != 45:
        raise RuntimeError("pandas sum")
    return "numpy %s, scipy linalg, pandas %s" % (np.__version__, pd.__version__)


def t_matplotlib_pillow():
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image
    fig, ax = plt.subplots(figsize=(2, 2))
    ax.plot([0, 1, 2], [0, 1, 4])
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf)
    img.load()
    if img.size[0] <= 0:
        raise RuntimeError("empty image")
    return "rendered %dx%d PNG in memory" % img.size


def t_crypto():
    import bcrypt
    import nacl.secret
    import nacl.utils
    from Crypto.Cipher import AES
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from OpenSSL import crypto as ossl_crypto  # noqa: F401
    from argon2 import PasswordHasher

    plain = b"madd-python smoke test"
    f = Fernet(Fernet.generate_key())
    if f.decrypt(f.encrypt(plain)) != plain:
        raise RuntimeError("Fernet roundtrip")
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    if AESGCM(key).decrypt(nonce, AESGCM(key).encrypt(nonce, plain, None), None) != plain:
        raise RuntimeError("AES-GCM roundtrip")
    box = nacl.secret.SecretBox(nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE))
    if box.decrypt(box.encrypt(plain)) != plain:
        raise RuntimeError("NaCl SecretBox roundtrip")
    k2 = os.urandom(16)
    c = AES.new(k2, AES.MODE_GCM)
    ct, tag = c.encrypt_and_digest(plain)
    if AES.new(k2, AES.MODE_GCM, nonce=c.nonce).decrypt_and_verify(ct, tag) != plain:
        raise RuntimeError("PyCryptodome roundtrip")
    pw = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4))
    if not bcrypt.checkpw(b"pw", pw):
        raise RuntimeError("bcrypt")
    ph = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    if not ph.verify(ph.hash("pw"), "pw"):
        raise RuntimeError("argon2")
    return "Fernet, AES-GCM, NaCl, PyCryptodome, bcrypt, argon2 (in-memory keys)"


def t_requests_ca():
    import ssl
    import certifi
    import httpx  # noqa: F401
    import requests
    ca = certifi.where()
    if not os.path.isfile(ca):
        raise RuntimeError("certifi bundle missing")
    ctx = ssl.create_default_context(cafile=ca)
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        raise RuntimeError("no CA certs loaded from certifi")
    s = requests.Session()
    s.close()
    return "requests %s, CA bundle %s (no request made)" % (requests.__version__, ca)


def t_misc():
    import io
    import psutil
    import rich.console
    import serial
    import yaml
    if yaml.safe_load(yaml.safe_dump({"a": 1})) != {"a": 1}:
        raise RuntimeError("yaml roundtrip")
    rich.console.Console(file=io.StringIO()).print("[bold]ok[/bold]")
    if psutil.cpu_count() < 1:
        raise RuntimeError("psutil")
    serial.serial_for_url("loop://", timeout=1).close()
    return "yaml, rich, psutil, pyserial loop://"


def main():
    vp.run_stdlib_checks()
    for name, func in [
        ("third-party imports", t_imports),
        ("numpy/scipy/pandas", t_numpy_scipy_pandas),
        ("matplotlib/Pillow", t_matplotlib_pillow),
        ("cryptography stack", t_crypto),
        ("requests/httpx/CA", t_requests_ca),
        ("yaml/rich/psutil/serial", t_misc),
    ]:
        vp.check(name, func)
    return vp.report("madd-python environment smoke test")


if __name__ == "__main__":
    sys.exit(main())
