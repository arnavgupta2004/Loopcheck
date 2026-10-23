#!/usr/bin/env bash
# Build the Lambda deployment zip (zip packaging, no Docker required). LOCAL ONLY: no AWS calls.
#
#   bash deploy/build_lambda_zip.sh                  # x86_64 (default; what template.yaml deploys)
#   ARCH=arm64 bash deploy/build_lambda_zip.sh       # aarch64 wheels (not import-verifiable on x86_64)
#
# Produces deploy/.build/loopcheck-lambda-<arch>.zip (template.yaml CodeUri) and checks:
#   * Lambda size limit (250 MiB unzipped),
#   * required native files are INSIDE the zip (cv2.abi3.so, numpy C extensions),
#   * x86_64 + Python 3.13 hosts: the zip is extracted to a temp dir and imported with an
#     isolated interpreter; OpenCV must be 5.x and the handler's GET /health must return 200.
set -euo pipefail

ARCH="${ARCH:-x86_64}"
case "$ARCH" in
  x86_64) PLATFORM="x86_64-manylinux_2_28" ;;
  arm64)  PLATFORM="aarch64-manylinux_2_28" ;;
  *) echo "ARCH must be x86_64 or arm64" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/deploy/.build"
PKG="$BUILD/lambda-$ARCH"
ZIP="$BUILD/loopcheck-lambda-$ARCH.zip"
rm -rf "$PKG" "$ZIP" && mkdir -p "$PKG"

echo "== installing wheels for cp313 / $PLATFORM (binary only)"
uv pip install --quiet --target "$PKG" --python-version 3.13 --python-platform "$PLATFORM" \
  --only-binary :all: -r "$ROOT/deploy/requirements-lambda.txt"

echo "== copying loopcheck package"
mkdir -p "$PKG/loopcheck"
cp "$ROOT"/loopcheck/*.py "$PKG/loopcheck/"

echo "== pruning files not needed at runtime"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "$PKG"/numpy/*/tests "$PKG"/numpy/tests "$PKG"/numpy/_pyinstaller "$PKG"/cv2/data 2>/dev/null || true
find "$PKG" -name "*.pyi" -delete

UNZIPPED=$(du -sb "$PKG" | cut -f1)
python3 -B -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" "${ZIP%.zip}" "$PKG"
ZIPPED=$(stat -c %s "$ZIP")
echo "== unzipped: $((UNZIPPED / 1048576)) MiB (Lambda limit 250 MiB incl. layers)"
echo "== zip:      $((ZIPPED / 1048576)) MiB (> 50 MiB direct-upload limit; SAM deploys it via S3)"
if [ "$UNZIPPED" -ge $((250 * 1048576)) ]; then echo "ERROR: exceeds the 250 MiB unzipped limit" >&2; exit 1; fi

echo "== checking required native files inside the zip"
python3 -B - "$ZIP" <<'EOF'
import sys, zipfile, re
names = zipfile.ZipFile(sys.argv[1]).namelist()
need = {"cv2/cv2.abi3.so": lambda n: n == "cv2/cv2.abi3.so",
        "numpy C extension (_multiarray_umath)": lambda n: re.match(r"numpy/_core/_multiarray_umath.*\.so$", n),
        "loopcheck/aws_lambda.py": lambda n: n == "loopcheck/aws_lambda.py"}
missing = [k for k, f in need.items() if not any(f(n) for n in names)]
pyc = [n for n in names if "__pycache__" in n]
print(f"   {len(names)} entries; required present: {not missing}; __pycache__ entries: {len(pyc)}")
sys.exit(1 if missing else 0)
EOF

if [ "$ARCH" = "x86_64" ] && [ "$(uname -m)" = "x86_64" ] && python3 -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
  echo "== verifying the zip: extract to a temp dir and import with an isolated interpreter"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  python3 -B -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$ZIP" "$TMP"
  (cd "$TMP" && PYTHONDONTWRITEBYTECODE=1 python3 -I -B -c '
import sys, os, json; sys.path.insert(0, os.getcwd())
import cv2, numpy
assert cv2.__file__.startswith(os.getcwd()), cv2.__file__
from loopcheck.aws_lambda import handler
out = handler({"rawPath": "/health", "requestContext": {"http": {"method": "GET"}}})
body = json.loads(out["body"])
print("   cv2", cv2.__version__, "| numpy", numpy.__version__, "| /health", out["statusCode"], body["status"])
assert out["statusCode"] == 200 and cv2.__version__.startswith("5.")
')
else
  echo "== skipping import check (needs an x86_64 host with Python 3.13 for this ARCH)"
fi
sha=$(sha256sum "$ZIP" | cut -d" " -f1)
echo "== done: $ZIP"
echo "   sha256 $sha"
