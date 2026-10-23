#!/usr/bin/env bash
# Build the Lambda deployment zip (zip packaging, no Docker required). LOCAL ONLY: no AWS calls.
# Portable: Linux and macOS (bash 3.2+). All size/hash/zip work is done by
# deploy/lambda_package.py (Python standard library), not GNU du/stat/sha256sum.
#
#   bash deploy/build_lambda_zip.sh                  # x86_64 (default; what template.yaml deploys)
#   ARCH=arm64 bash deploy/build_lambda_zip.sh       # aarch64 build (template.yaml does not use it)
#   PYTHON=/path/to/python3 bash deploy/build_lambda_zip.sh   # helper interpreter (>= 3.8; any OS)
#
# The wheels are always the Linux ones for Lambda (cp313, manylinux_2_28) — never the host's
# macOS wheels — installed by uv with --python-platform/--python-version/--only-binary.
# Output: deploy/.build/loopcheck-lambda-<arch>.zip. It is written under a temporary name and
# moved into place ONLY after every check passes; on any failure no zip is left behind.
set -euo pipefail

ARCH="${ARCH:-x86_64}"
case "$ARCH" in
  x86_64) PLATFORM="x86_64-manylinux_2_28" ;;
  arm64)  PLATFORM="aarch64-manylinux_2_28" ;;
  *) echo "ERROR: ARCH must be x86_64 or arm64" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/deploy/.build"
PKG="$BUILD/lambda-$ARCH"
ZIP="$BUILD/loopcheck-lambda-$ARCH.zip"
PARTIAL="$ZIP.partial"
HELPER="$ROOT/deploy/lambda_package.py"
REQS="$ROOT/deploy/requirements-lambda.txt"
PY="${PYTHON:-python3}"

finish() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    rm -f "$ZIP" "$PARTIAL"
    echo "BUILD FAILED (exit $rc): no deployment zip was left at $ZIP" >&2
  fi
}
trap finish EXIT

# Prerequisites (fail early and clearly)
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found (https://docs.astral.sh/uv/)" >&2; exit 1; }
command -v "$PY" >/dev/null 2>&1 || { echo "ERROR: $PY not found; set PYTHON=/path/to/python3" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "ERROR: $PY must be Python >= 3.8" >&2; exit 1; }

rm -rf "$PKG" "$ZIP" "$PARTIAL"   # never leave a stale zip from an earlier build
mkdir -p "$PKG"

echo "== installing Linux wheels for cp313 / $PLATFORM (binary only; host: $(uname -s) $(uname -m))"
uv pip install --quiet --target "$PKG" --python-version 3.13 --python-platform "$PLATFORM" \
  --only-binary :all: -r "$REQS"

echo "== copying loopcheck package"
mkdir -p "$PKG/loopcheck"
cp "$ROOT"/loopcheck/*.py "$PKG/loopcheck/"

echo "== pruning files not needed at runtime"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$PKG" -name ".DS_Store" -exec rm -f {} +
rm -rf "$PKG"/numpy/*/tests "$PKG"/numpy/tests "$PKG"/numpy/_pyinstaller "$PKG"/cv2/data
find "$PKG" -name "*.pyi" -exec rm -f {} +

UNZIPPED=$("$PY" -B "$HELPER" dir-size "$PKG")
echo "== unzipped: $((UNZIPPED / 1048576)) MiB (Lambda limit 250 MiB incl. layers)"
if [ "$UNZIPPED" -ge $((250 * 1048576)) ]; then echo "ERROR: exceeds the 250 MiB unzipped limit" >&2; exit 1; fi

echo "== writing zip"
"$PY" -B "$HELPER" make-zip "$PKG" "$PARTIAL" >/dev/null

echo "== verifying the archive (structure, Linux ELF $ARCH binaries, versions, size)"
"$PY" -B "$HELPER" verify "$PARTIAL" --requirements "$REQS" --arch "$ARCH" --source "$ROOT/loopcheck" | sed 's/^/   /'

if [ "$ARCH" = "x86_64" ]; then
  echo "== import smoke test (extract + isolated import + GET /health)"
  "$PY" -B "$HELPER" smoke "$PARTIAL" | sed 's/^/   /'
fi

mv "$PARTIAL" "$ZIP"
echo "== done: $ZIP"
echo "   sha256 $("$PY" -B "$HELPER" sha256 "$ZIP")"
