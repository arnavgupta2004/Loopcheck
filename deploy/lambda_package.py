#!/usr/bin/env python3
"""Portable helpers for building and checking the Lambda deployment zip.

Standard library only, Python >= 3.8, works on Linux and macOS (no GNU du/stat/sha256sum).

    python3 deploy/lambda_package.py dir-size DIR
    python3 deploy/lambda_package.py make-zip DIR OUT.zip
    python3 deploy/lambda_package.py verify OUT.zip --requirements deploy/requirements-lambda.txt [--arch x86_64] [--source loopcheck]
    python3 deploy/lambda_package.py smoke OUT.zip          # import + GET /health; Linux x86_64 + Python 3.13 only
    python3 deploy/lambda_package.py sha256 FILE

`verify` checks the archive *structurally*, so it also works on a Mac that cannot import
Linux binaries: CRCs, required compiled files, that they are Linux ELF binaries for the
Lambda architecture (a macOS Mach-O binary is rejected), package versions against the
pinned requirements, no conflicting OpenCV distributions, no __pycache__/macOS junk, and the
250 MiB unzipped Lambda limit. Exit codes: 0 ok, 1 verification failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile

LAMBDA_UNZIPPED_LIMIT = 250 * 1024 * 1024
ELF_MACHINE = {"x86_64": 0x3E, "arm64": 0xB7}
NUMPY_TAG = {"x86_64": "x86_64-linux-gnu", "arm64": "aarch64-linux-gnu"}
MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe"}


def dir_size(path: str) -> int:
    """Total bytes of regular files under path (symlinks not followed)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            st = os.lstat(os.path.join(root, name))
            total += st.st_size
    return total


def make_zip(src: str, out: str) -> int:
    """Zip src's contents (sorted, deflated, Unix permissions kept). Returns entry count."""
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(src):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, src))
                n += 1
    return n


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pins(requirements: str) -> dict:
    pins = {}
    with open(requirements) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if "==" in line:
                name, ver = line.split("==", 1)
                pins[name.strip().lower().replace("-", "_")] = ver.strip()
    return pins


def _binary_kind(head: bytes, arch: str) -> str | None:
    """None if `head` is a 64-bit little-endian ELF for `arch`, else a description of the problem."""
    if head[:4] in MACHO_MAGICS:
        return "a macOS (Mach-O) binary — wrong platform for Lambda"
    if head[:4] != b"\x7fELF":
        return "not an ELF binary"
    if head[4] != 2 or head[5] != 1:
        return "not a 64-bit little-endian ELF"
    machine = int.from_bytes(head[18:20], "little")
    if machine != ELF_MACHINE[arch]:
        return f"ELF for machine 0x{machine:x}, expected {arch} (0x{ELF_MACHINE[arch]:x})"
    return None


def verify(zip_path: str, requirements: str, arch: str = "x86_64", source: str | None = None) -> tuple[list, list, dict]:
    """Return (errors, warnings, info)."""
    errors, warnings = [], []
    info = {"zip": zip_path, "arch": arch}
    if not os.path.isfile(zip_path):
        return [f"zip not found: {zip_path}"], [], info
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        return [f"not a valid zip: {exc}"], [], info
    with z:
        bad = z.testzip()
        if bad:
            errors.append(f"CRC/corruption error in {bad}")
        names = z.namelist()
        info.update(entries=len(names), zipped_bytes=os.path.getsize(zip_path),
                    unzipped_bytes=sum(i.file_size for i in z.infolist()), sha256=sha256(zip_path))
        if info["unzipped_bytes"] >= LAMBDA_UNZIPPED_LIMIT:
            errors.append(f"unzipped size {info['unzipped_bytes'] // 1048576} MiB >= 250 MiB Lambda limit")

        required = {
            "OpenCV binary cv2/cv2.abi3.so": [n for n in names if n == "cv2/cv2.abi3.so"],
            "NumPy C extension numpy/_core/_multiarray_umath*.so": [n for n in names if re.match(r"numpy/_core/_multiarray_umath.*\.so$", n)],
            "loopcheck/aws_lambda.py": [n for n in names if n == "loopcheck/aws_lambda.py"],
            "loopcheck/__init__.py": [n for n in names if n == "loopcheck/__init__.py"],
        }
        for label, hits in required.items():
            if not hits:
                errors.append(f"missing {label}")
        for label in ("OpenCV binary cv2/cv2.abi3.so", "NumPy C extension numpy/_core/_multiarray_umath*.so"):
            for n in required[label]:
                with z.open(n) as f:
                    problem = _binary_kind(f.read(20), arch)
                if problem:
                    errors.append(f"{n} is {problem}")
        for n in required["NumPy C extension numpy/_core/_multiarray_umath*.so"]:
            if "cpython-313" not in n or NUMPY_TAG[arch] not in n:
                errors.append(f"{n} is not built for CPython 3.13 on {NUMPY_TAG[arch]}")

        pins = _pins(requirements)
        dists = {}
        for n in names:
            m = re.match(r"([^/]+)-([^/-]+)\.dist-info/METADATA$", n)
            if m:
                dists[m.group(1).lower()] = m.group(2)
        info["distributions"] = dists
        for name, ver in pins.items():
            if dists.get(name) != ver:
                errors.append(f"{name}: expected {ver}, found {dists.get(name, 'nothing')}")
        cv = dists.get("opencv_contrib_python_headless", "")
        if not cv.startswith("5."):
            errors.append(f"OpenCV distribution is not 5.x (found {cv or 'none'})")
        others = [d for d in dists if d.startswith("opencv") and d != "opencv_contrib_python_headless"]
        if others:
            errors.append(f"conflicting OpenCV distributions in the zip: {others}")
        for n in names:
            if n.endswith(".dist-info/WHEEL"):
                tags = z.read(n).decode("utf-8", "replace")
                plat = "x86_64" if arch == "x86_64" else "aarch64"
                if "Root-Is-Purelib: false" in tags and not ("manylinux" in tags and plat in tags):
                    errors.append(f"{n}: binary wheel is not manylinux/{plat}")
        junk = [n for n in names if "__pycache__" in n or n.startswith("__MACOSX/") or n.endswith(".DS_Store")]
        if junk:
            errors.append(f"{len(junk)} junk entries (e.g. {junk[0]})")

        if source:
            stale = []
            for fn in sorted(os.listdir(source)):
                if fn.endswith(".py"):
                    with open(os.path.join(source, fn), "rb") as f:
                        src_bytes = f.read()
                    if f"loopcheck/{fn}" not in names or z.read(f"loopcheck/{fn}") != src_bytes:
                        stale.append(fn)
            if stale:
                warnings.append(f"packaged loopcheck/ differs from source: {', '.join(stale)} — rebuild")
    return errors, warnings, info


SMOKE = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
import cv2, numpy
assert cv2.__file__.startswith(os.getcwd()), cv2.__file__
from loopcheck.aws_lambda import handler
out = handler({"rawPath": "/health", "requestContext": {"http": {"method": "GET"}}})
body = json.loads(out["body"])
print(json.dumps({"cv2": cv2.__version__, "numpy": numpy.__version__, "health_status": out["statusCode"], "health": body.get("status")}))
sys.exit(0 if out["statusCode"] == 200 and cv2.__version__.startswith("5.") else 1)
"""


def smoke(zip_path: str) -> int:
    """Extract and import the zip with an isolated interpreter. Only meaningful where the
    packaged Linux binaries can load: Linux x86_64 with CPython 3.13."""
    if not (sys.platform.startswith("linux") and platform.machine() in ("x86_64", "AMD64") and sys.version_info[:2] == (3, 13)):
        print(f"SKIPPED import smoke test: host is {sys.platform}/{platform.machine()}/Python {sys.version.split()[0]} "
              "(Linux x86_64 + Python 3.13 required to load the Lambda binaries); structural verification still applies")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, "-I", "-B", "-c", SMOKE], cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
        print(p.stdout.strip() or p.stderr.strip()[-2000:])
        return p.returncode


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("dir-size"); s.add_argument("path")
    s = sub.add_parser("make-zip"); s.add_argument("src"); s.add_argument("out")
    s = sub.add_parser("sha256"); s.add_argument("path")
    s = sub.add_parser("smoke"); s.add_argument("zip")
    s = sub.add_parser("verify"); s.add_argument("zip"); s.add_argument("--requirements", required=True)
    s.add_argument("--arch", choices=sorted(ELF_MACHINE), default="x86_64"); s.add_argument("--source"); s.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "dir-size":
        print(dir_size(a.path)); return 0
    if a.cmd == "make-zip":
        print(make_zip(a.src, a.out)); return 0
    if a.cmd == "sha256":
        print(sha256(a.path)); return 0
    if a.cmd == "smoke":
        return smoke(a.zip)
    errors, warnings, info = verify(a.zip, a.requirements, a.arch, a.source)
    if a.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings, **info}))
    else:
        if "entries" in info:
            print(f"INFO {info['entries']} entries, {info['unzipped_bytes'] // 1048576} MiB unzipped, "
                  f"{info['zipped_bytes'] // 1048576} MiB zipped, sha256 {info['sha256']}")
            print("INFO distributions: " + ", ".join(f"{k}=={v}" for k, v in sorted(info.get("distributions", {}).items())))
        for w in warnings:
            print(f"WARN {w}")
        for e in errors:
            print(f"ERROR {e}")
        print("VERIFY: " + ("PASS" if not errors else f"FAIL ({len(errors)} error(s))"))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
