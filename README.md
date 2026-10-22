# LoopCheck

Agentic visual inspection for the OpenCV AI Competition 2026: compare a reference
image of an assembly or packed tray against an inspection image, find missing,
misplaced or anomalous components, and decide whether to inspect further,
approve, flag a defect or request human review.

**Status**
- D1 (OpenCV 5 environment verification): complete.
- D2 (deterministic registration and comparison pipeline, CLI, synthetic evaluation): complete.
- Not built yet: the agent controller, AWS deployment and a UI.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+ (verified on 3.13.16, Linux x86_64).

```bash
uv sync --frozen                                   # creates .venv from uv.lock (incl. pytest)
uv run --frozen python scripts/check_opencv5.py    # OpenCV 5 smoke tests; exit 0 = all pass
```

Without uv: `python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest`.

Install **only** `opencv-contrib-python-headless==5.0.0.93`. Every `opencv-*`
wheel ships the same `cv2` module, so a second one (e.g. `opencv-python` 4.x
pulled in by another library) silently overwrites it. Both the check script and
`import loopcheck` fail if `cv2.__version__` does not start with `5.`.

## Run an inspection

```bash
uv run --frozen python -m loopcheck.cli \
  --reference data/demo/reference.png \
  --inspection data/demo/inspection.png \
  --output-dir outputs/demo
```

Output goes to `outputs/demo/`:

| File | Contents |
|---|---|
| `report.json` | status, registration metrics and homography, comparison diagnostics, candidate regions with boxes, scores and evidence |
| `overlay.png` | candidate regions drawn on the inspection image |
| `heatmap.png` | anomaly map over the aligned image |
| `aligned.png` | inspection image warped into the reference frame |
| `anomaly_map.png` | raw fused anomaly map |

`status` is one of `analyzed` (exit code 0), `input_error` (2) or `registration_failed` (3).
A failed registration never produces regions or a verdict.

Options:
- `--config cfg.json` overrides any field of `loopcheck/config.py`.
- `--ecc` enables ECC refinement, which is off by default; see the docs for why.

The demo pair's ground truth is in `data/demo/ground_truth.json`.

## Tests and evaluation

```bash
uv run --frozen pytest -q                                                            # unit + integration tests

uv run --frozen python -m loopcheck.synth --out data/synthetic/dev  --seed 1000 --per-type 8
uv run --frozen python -m loopcheck.synth --out data/synthetic/test --seed 2000 --per-type 10
uv run --frozen python -m loopcheck.evaluate --data data/synthetic/dev  --out results/eval_dev
uv run --frozen python -m loopcheck.evaluate --data data/synthetic/test --out results/eval_test
```

Generated images (`data/synthetic/`) are not committed. They regenerate byte-for-byte
from the seeds. Evaluation outputs are committed under `results/`.

## Layout

```
loopcheck/      config, imageio, registration, compare, pipeline, cli, synth, evaluate
tests/          pytest suite
scripts/        check_opencv5.py (D1)
docs/           opencv5_verification.md (D1), d2_pipeline.md (D2 design, results, failure cases)
results/        opencv5_check.json, eval_dev/, eval_test/
data/demo/      demo reference/inspection pair + ground truth
```
