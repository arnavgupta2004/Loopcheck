"""Portable Lambda packaging: deploy/lambda_package.py and deploy/build_lambda_zip.sh."""

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_deploy import ELF_AARCH64, MACHO_ARM64, NUMPY_PIN, fake_lambda_zip

ROOT = Path(__file__).resolve().parents[1]
REQS = str(ROOT / "deploy" / "requirements-lambda.txt")

spec = importlib.util.spec_from_file_location("lambda_package", ROOT / "deploy" / "lambda_package.py")
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)


# --------------------------------------------------------------------------- helper: portable primitives
def test_dir_size_and_make_zip_roundtrip(tmp_path):
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "x.bin").write_bytes(b"x" * 1000)
    (src / "a" / "b" / "y.so").write_bytes(b"y" * 2345)
    (src / "a" / "b" / "y.so").chmod(0o755)
    assert lp.dir_size(str(src)) == 3345
    out = tmp_path / "o.zip"
    assert lp.make_zip(str(src), str(out)) == 2
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["a/b/y.so", "a/x.bin"]
        assert (z.getinfo("a/b/y.so").external_attr >> 16) & 0o777 == 0o755  # permissions preserved
    assert re.fullmatch(r"[0-9a-f]{64}", lp.sha256(str(out)))


def test_helper_runs_no_external_tools_and_parses_as_python38():
    src = (ROOT / "deploy" / "lambda_package.py").read_text()
    calls = re.findall(r"subprocess\.\w+\(\s*\[([^,\]]+)", src)
    assert calls == ["sys.executable"]  # only ever re-invokes Python itself (import smoke test)
    assert "os.system" not in src and "shell=True" not in src
    import ast
    ast.parse(src, feature_version=(3, 8))  # e.g. macOS's system python3 can run it


# --------------------------------------------------------------------------- helper: verification
def test_verify_accepts_a_correct_linux_x86_64_package(tmp_path):
    errors, warnings, info = lp.verify(str(fake_lambda_zip(tmp_path / "ok.zip")), REQS, "x86_64", str(ROOT / "loopcheck"))
    assert errors == [] and warnings == [] and info["distributions"]["opencv_contrib_python_headless"] == "5.0.0.93"


@pytest.mark.parametrize("kwargs, message", [
    ({"omit": ("cv2/cv2.abi3.so",)}, "missing OpenCV binary cv2/cv2.abi3.so"),
    ({"omit": ("numpy/_core/",)}, "missing NumPy C extension"),
    ({"omit": ("loopcheck/aws_lambda.py",)}, "missing loopcheck/aws_lambda.py"),
    ({"cv2_bytes": MACHO_ARM64}, "macOS (Mach-O) binary"),
    ({"numpy_bytes": MACHO_ARM64}, "macOS (Mach-O) binary"),
    ({"cv2_bytes": ELF_AARCH64}, "expected x86_64"),
    ({"cv2_bytes": b"PK\x03\x04" + b"\0" * 40}, "not an ELF binary"),
    ({"numpy_name": "numpy/_core/_multiarray_umath.cpython-313-darwin.so"}, "not built for CPython 3.13 on x86_64-linux-gnu"),
    ({"numpy_name": "numpy/_core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so"}, "not built for CPython 3.13"),
    ({"cv_version": "4.10.0.84"}, "OpenCV distribution is not 5.x"),
    ({"numpy_version": "1.26.4"}, f"numpy: expected {NUMPY_PIN}"),
    ({"wheel": "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-macosx_11_0_arm64\n"}, "not manylinux/x86_64"),
    ({"extra": {"opencv_python-4.10.0.84.dist-info/METADATA": b"Version: 4.10.0.84\n"}}, "conflicting OpenCV distributions"),
    ({"extra": {"loopcheck/__pycache__/x.pyc": b""}}, "junk entries"),
    ({"extra": {"__MACOSX/._cv2": b""}}, "junk entries"),
])
def test_verify_rejects_broken_or_wrong_platform_packages(tmp_path, kwargs, message):
    errors, _, _ = lp.verify(str(fake_lambda_zip(tmp_path / "bad.zip", **kwargs)), REQS, "x86_64")
    assert any(message in e for e in errors), errors


def test_verify_rejects_missing_and_corrupt_files(tmp_path):
    assert lp.verify(str(tmp_path / "nope.zip"), REQS)[0] == [f"zip not found: {tmp_path / 'nope.zip'}"]
    trunc = tmp_path / "trunc.zip"
    trunc.write_bytes(fake_lambda_zip(tmp_path / "full.zip").read_bytes()[:500])
    assert lp.verify(str(trunc), REQS)[0][0].startswith("not a valid zip")


def test_verify_enforces_the_lambda_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "LAMBDA_UNZIPPED_LIMIT", 100)
    errors, _, _ = lp.verify(str(fake_lambda_zip(tmp_path / "big.zip")), REQS)
    assert any("250 MiB Lambda limit" in e for e in errors)


def test_verify_warns_when_packaged_code_is_stale(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(ROOT / "loopcheck", src, ignore=shutil.ignore_patterns("__pycache__"))
    z = fake_lambda_zip(tmp_path / "ok.zip", source=src)
    (src / "agent.py").write_text((src / "agent.py").read_text() + "\n# changed\n")
    errors, warnings, _ = lp.verify(str(z), REQS, "x86_64", str(src))
    assert errors == [] and warnings and "agent.py" in warnings[0]


def test_verify_cli_exit_codes(tmp_path):
    good, bad = fake_lambda_zip(tmp_path / "g.zip"), fake_lambda_zip(tmp_path / "b.zip", cv2_bytes=MACHO_ARM64)
    run = lambda z: subprocess.run([sys.executable, "-B", str(ROOT / "deploy" / "lambda_package.py"), "verify", str(z), "--requirements", REQS],  # noqa: E731
                                   capture_output=True, text=True)
    g, b = run(good), run(bad)
    assert g.returncode == 0 and "VERIFY: PASS" in g.stdout
    assert b.returncode == 1 and "VERIFY: FAIL" in b.stdout and "ERROR" in b.stdout


def test_smoke_test_skips_cleanly_on_macos(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lp.sys, "platform", "darwin")
    assert lp.smoke(str(fake_lambda_zip(tmp_path / "ok.zip"))) == 0
    assert "SKIPPED import smoke test" in capsys.readouterr().out


# --------------------------------------------------------------------------- build script
BUILD = (ROOT / "deploy" / "build_lambda_zip.sh").read_text()


def test_build_script_uses_no_gnu_only_commands():
    code = "\n".join(ln for ln in BUILD.splitlines() if not ln.lstrip().startswith("#"))
    for pattern in (r"\bdu\b", r"\bstat\s+-", r"\bsha256sum\b", r"\bshasum\b", r"readlink\s+-f", r"sed\s+-i", r"\bcp\s+-t\b",
                    r"date\s+-d", r"grep\s+-P", r"xargs\s+-r", r"-printf", r"\bnumfmt\b", r"\btac\b"):
        assert not re.search(pattern, code), pattern
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / "build_lambda_zip.sh")]).returncode == 0


def _sandbox(tmp_path):
    (tmp_path / "deploy").mkdir()
    for f in ("build_lambda_zip.sh", "lambda_package.py", "requirements-lambda.txt"):
        shutil.copy(ROOT / "deploy" / f, tmp_path / "deploy" / f)
    shutil.copytree(ROOT / "loopcheck", tmp_path / "loopcheck", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "bin").mkdir()
    return tmp_path


def _exe(path: Path, text: str):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _bsd_shims(bin_dir: Path):
    """Make du/stat/sha256sum behave like macOS: GNU options rejected, sha256sum absent."""
    _exe(bin_dir / "du", "#!/bin/sh\nfor a in \"$@\"; do case \"$a\" in -*b*) echo 'du: invalid option -- b' >&2; exit 1;; esac; done\nexec /usr/bin/du \"$@\"\n")
    _exe(bin_dir / "stat", "#!/bin/sh\nfor a in \"$@\"; do case \"$a\" in -c) echo 'stat: illegal option -- c' >&2; exit 1;; esac; done\nexec /usr/bin/stat \"$@\"\n")
    _exe(bin_dir / "sha256sum", "#!/bin/sh\necho 'sha256sum: command not found' >&2; exit 127\n")


def _run_build(sb: Path, extra_env=None, timeout=600):
    env = dict(os.environ, PATH=f"{sb / 'bin'}:{os.environ['PATH']}", **(extra_env or {}))
    return subprocess.run(["bash", str(sb / "deploy" / "build_lambda_zip.sh")], capture_output=True, text=True, env=env, timeout=timeout)


def test_failed_install_leaves_no_zip_and_removes_stale_one(tmp_path):
    sb = _sandbox(tmp_path)
    (sb / "deploy" / ".build").mkdir()
    stale = sb / "deploy" / ".build" / "loopcheck-lambda-x86_64.zip"
    fake_lambda_zip(stale)  # a valid-looking zip from an earlier build
    _exe(sb / "bin" / "uv", "#!/bin/sh\necho 'error: network unreachable' >&2; exit 2\n")
    p = _run_build(sb)
    assert p.returncode != 0 and "BUILD FAILED" in p.stderr
    assert not stale.exists() and not (sb / "deploy" / ".build" / "loopcheck-lambda-x86_64.zip.partial").exists()


FAKE_UV_MACOS = r"""#!/bin/sh
# mimics 'uv pip install --target DIR ...' but drops macOS (Mach-O) binaries, as a wrong-platform install would
while [ $# -gt 0 ]; do case "$1" in --target) T="$2"; shift;; esac; shift; done
mkdir -p "$T/cv2" "$T/numpy/_core" "$T/opencv_contrib_python_headless-5.0.0.93.dist-info" "$T/numpy-NUMPYVER.dist-info"
printf '\317\372\355\376' > "$T/cv2/cv2.abi3.so"
printf '\317\372\355\376' > "$T/numpy/_core/_multiarray_umath.cpython-313-darwin.so"
printf 'Version: 5.0.0.93\n' > "$T/opencv_contrib_python_headless-5.0.0.93.dist-info/METADATA"
printf 'Version: NUMPYVER\n' > "$T/numpy-NUMPYVER.dist-info/METADATA"
"""


def test_macos_native_binaries_are_rejected_and_no_zip_is_left(tmp_path):
    sb = _sandbox(tmp_path)
    _exe(sb / "bin" / "uv", FAKE_UV_MACOS.replace("NUMPYVER", NUMPY_PIN))
    p = _run_build(sb)
    assert p.returncode != 0 and "BUILD FAILED" in p.stderr
    assert "macOS (Mach-O) binary" in p.stdout and "VERIFY: FAIL" in p.stdout
    assert not list((sb / "deploy" / ".build").glob("*.zip*"))


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
@pytest.mark.skipif(os.environ.get("LOOPCHECK_SKIP_SLOW") == "1", reason="slow test disabled")
def test_full_build_succeeds_with_macos_style_tools(tmp_path):
    """Real build (downloads/uses the real Linux wheels) with BSD-like du/stat and no sha256sum."""
    sb = _sandbox(tmp_path)
    _bsd_shims(sb / "bin")
    p = _run_build(sb, timeout=900)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    z = sb / "deploy" / ".build" / "loopcheck-lambda-x86_64.zip"
    assert z.exists() and "VERIFY: PASS" in p.stdout and "sha256" in p.stdout
    assert "du: invalid option" not in p.stderr and "illegal option" not in p.stderr
    errors, _, info = lp.verify(str(z), REQS, "x86_64", str(sb / "loopcheck"))
    assert errors == [] and info["unzipped_bytes"] < 250 * 1024 * 1024
