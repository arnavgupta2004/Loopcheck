#!/usr/bin/env bash
# Build the Lambda deployment folder (zip packaging, no Docker required).
#
#   ARCH=x86_64 bash deploy/build_lambda_zip.sh      # default; arm64 also supported
#
# Produces deploy/.build/lambda/ (referenced by template.yaml CodeUri) and
# deploy/.build/loopcheck-lambda-<arch>.zip, then checks Lambda size limits.
# For ARCH=x86_64 on an x86_64 Linux host with Python 3.13 it also imports the packaged
# OpenCV and invokes the handler's /health route from the package directory.
set -euo pipefail

ARCH="${ARCH:-x86_64}"
case "$ARCH" in
  x86_64) PLATFORM="x86_64-manylinux_2_28" ;;
  arm64)  PLATFORM="aarch64-manylinux_2_28" ;;
  *) echo "ARCH must be x86_64 or arm64" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/deploy/.build"
PKG="$BUILD/lambda"
rm -rf "$PKG" && mkdir -p "$PKG"

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
ZIP="$BUILD/loopcheck-lambda-$ARCH.zip"
rm -f "$ZIP"
(cd "$PKG" && python3 -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', '.')" "${ZIP%.zip}")
ZIPPED=$(stat -c %s "$ZIP")
echo "== unzipped: $((UNZIPPED / 1048576)) MiB (Lambda limit 250 MiB incl. layers)"
echo "== zip:      $((ZIPPED / 1048576)) MiB (direct-upload limit 50 MiB; larger zips deploy via S3, as SAM does)"
if [ "$UNZIPPED" -ge $((250 * 1048576)) ]; then echo "ERROR: exceeds the 250 MiB unzipped limit" >&2; exit 1; fi

if [ "$ARCH" = "x86_64" ] && [ "$(uname -m)" = "x86_64" ] && python3 -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
  echo "== verifying the packaged code from the package directory (isolated interpreter)"
  (cd "$PKG" && python3 -I -c '
import sys, json; sys.path.insert(0, ".")
import cv2, numpy
assert cv2.__file__.startswith(sys.path[0]) or "lambda" in cv2.__file__, cv2.__file__
from loopcheck.aws_lambda import handler
out = handler({"rawPath": "/health", "requestContext": {"http": {"method": "GET"}}})
body = json.loads(out["body"])
print("   cv2", cv2.__version__, "from", cv2.__file__)
print("   numpy", numpy.__version__)
print("   /health ->", out["statusCode"], body["status"], body["opencv_version"])
assert out["statusCode"] == 200 and body["opencv_version"].startswith("5.")
')
else
  echo "== skipping local import check (needs x86_64 host + Python 3.13 for this ARCH)"
fi
echo "== done: $PKG"
