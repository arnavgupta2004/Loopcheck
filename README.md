# LoopCheck

Agentic visual inspection for the OpenCV AI Competition 2026: compare a reference
image of an assembly or packed tray against an inspection image, find missing,
misplaced or anomalous components, and decide whether to inspect further,
approve, flag a defect or request human review.

**Status:** D1 (environment verification) complete. The inspection pipeline is
not implemented yet.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+ (verified on 3.13.16, Linux x86_64).

```bash
uv sync --frozen                                   # creates .venv from uv.lock
uv run --frozen python scripts/check_opencv5.py    # OpenCV 5 smoke tests; exit 0 = all pass
```

Without uv: `python -m venv .venv && .venv/bin/pip install -r requirements.txt`.

Install **only** `opencv-contrib-python-headless==5.0.0.93`. Every `opencv-*`
wheel ships the same `cv2` module, so a second one (e.g. `opencv-python` 4.x
pulled in by another library) silently overwrites it. The check script fails if
`cv2.__version__` does not start with `5.`.

## OpenCV 5 verification

See [docs/opencv5_verification.md](docs/opencv5_verification.md) for the tested
operations and results; raw output is in `results/opencv5_check.json`.
