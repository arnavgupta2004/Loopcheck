# LoopCheck

Agentic visual inspection for the OpenCV AI Competition 2026: compare a reference
image of an assembly or packed tray against an inspection image, find missing,
misplaced or anomalous components, and decide whether to inspect further,
approve, flag a defect or request human review.

**Status**
- D1 (OpenCV 5 environment verification): complete.
- D2 (deterministic registration and comparison pipeline, CLI, synthetic evaluation): complete.
- D2.1 (exclude detected ArUco markers from anomaly scoring, validated on fresh synthetic splits): complete.
- D3 (adaptive perception-decision-action controller with a traced APPROVE / DEFECT /
  HUMAN_REVIEW decision): complete. See [docs/d3_agent.md](docs/d3_agent.md).
- D4.1 (hardened AWS configuration, read-only preflight, deployment runbook for ap-south-1): complete; not deployed.
- D4 (Streamlit demo UI + AWS Lambda/API Gateway backend): demo complete and verified
  locally. AWS backend **prepared but NOT deployed**: no cloud resources exist and there is
  no public URL. See [docs/d4_demo_and_deployment.md](docs/d4_demo_and_deployment.md).

## Demo UI (D4)

```bash
uv sync --frozen --group demo                          # adds Streamlit (not part of the Lambda package)
uv run --frozen --group demo streamlit run app/streamlit_app.py
# open http://localhost:8501
```

- **Inputs:** pick one of 8 bundled **synthetic** cases (clean, missing part, shifted part,
  lighting change, ambiguous tiny object, defect next to a marker, different tray,
  registration failure), or upload two PNG/JPEG images (≤ 2 MB, ≤ 4096 px).
- **What it shows:** the decision, the step-by-step agent trace, regions with re-inspection
  evidence, registration and coverage (excluded marker areas are labelled NOT inspected),
  latency and limitations.
- **Downloads:** the JSON report and the annotated overlay.
- **Backend:** "Local (in-process)" runs the real controller in the Streamlit process.
  "HTTP API" calls `LOOPCHECK_API_URL`: the deployed API, or the local stand-in below.

```bash
uv run --frozen python -m loopcheck.local_server --port 8000   # same handler code as Lambda
LOOPCHECK_API_URL=http://127.0.0.1:8000 uv run --frozen --group demo streamlit run app/streamlit_app.py
```

## AWS backend (prepared, NOT deployed)

The target is stack `loopcheck-demo` in `ap-south-1`, deployed with AWS profile `loopcheck`
from your own machine. The full copy-paste runbook, including verification, logs and safe
cleanup, is in [docs/d4_demo_and_deployment.md](docs/d4_demo_and_deployment.md#runbook-deploy-from-your-own-machine).

```bash
# LOCAL, free: build + verify the zip, validate, read-only preflight
bash deploy/build_lambda_zip.sh
sam validate --lint --template template.yaml --region ap-south-1 --profile loopcheck
bash deploy/preflight.sh                       # read-only; prints account/region; never prints credentials
# BILLABLE: creates resources (shows a change set first)
sam deploy --stack-name loopcheck-demo --region ap-south-1 --profile loopcheck --resolve-s3 --s3-prefix loopcheck-demo \
  --capabilities CAPABILITY_IAM --confirm-changeset --parameter-overrides AllowedOrigin=http://localhost:8501 ConcurrencyMode=reserved
# (if the preflight recommends it: ConcurrencyMode=unreserved, which omits the reservation; never use 0)
# verify on AWS, then clean up
uv run --frozen python -m loopcheck.client --url "<ApiUrl output>" --expect DEFECT
sam delete --stack-name loopcheck-demo --region ap-south-1 --profile loopcheck
```

Cost controls: API throttling at 2 requests/s with burst 5, reserved concurrency 2 when the
account supports it (otherwise unreserved mode, which omits the reservation),
logging-only IAM, logs containing metadata only, CORS limited to one origin (CORS is not
authentication). These reduce risk but **cannot guarantee a maximum bill**.

The API is `GET /health` and `POST /inspect` with
`{"reference_b64", "inspection_b64", "options": {"include_images"}}`. The response carries
the decision, the full trace and JPEG images. Every error response carries
`"decision": "HUMAN_REVIEW"`. Templates and packaging live in `template.yaml` and `deploy/`.

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

Detected ArUco markers, with their white quiet zone and a small margin, are excluded
from anomaly scoring by default. They appear as grey outlines on `overlay.png` and as
`report.json["marker_masking"]`. **Nothing inside that band is inspected**: it reaches
about 22 px past a 56 px marker's edge.

Options:
- `--config cfg.json` overrides any field of `loopcheck/config.py`.
- `--config configs/baseline_no_marker_mask.json` turns marker masking off (D2 behaviour).
- `--ecc` enables ECC refinement, which is off by default; see the docs for why.

The demo pair's ground truth is in `data/demo/ground_truth.json`.

### Adaptive agent (D3)

```bash
uv run --frozen python -m loopcheck.cli \
  --reference data/demo/reference.png \
  --inspection data/demo/inspection.png \
  --output-dir outputs/demo_agent --agent
```

The agent observes OpenCV results and chooses its next tool from explicit rules. Its
possible actions:
- alternative registration
- illumination-robust comparison
- targeted high-sensitivity scan
- multi-view crop re-inspection
- marker-band diagnostic
- content-identity check

It prints a step-by-step trace and ends with `APPROVE` (exit 0), `DEFECT` (4) or
`HUMAN_REVIEW` (5; input errors included). Any error, exhausted budget or unresolved
evidence leads to HUMAN_REVIEW, never APPROVE.

Output files:
- `report.json`: decision, reasons, candidates with every re-inspection view, coverage,
  excluded (not inspected) polygons, and the full trace.
- `overlay.png`: candidates coloured by status, and excluded areas.

Options:
- `--agent-config file.json` overrides `AgentConfig`.
- `--quiet` prints only the decision.

## Tests and evaluation

```bash
uv run --frozen --group demo pytest -q                      # all tests (UI tests are skipped without the demo group)
uv run --frozen python scripts/check_opencv5.py             # D1 OpenCV 5 verification
```

**D3 evaluation** (one-shot D2.1 vs adaptive agent on the same cases, fresh splits):

```bash
uv run --frozen python -m loopcheck.synth --out data/synthetic/d3_dev  --seed 4100 --per-type 8
uv run --frozen python -m loopcheck.synth --out data/synthetic/d3_test --seed 4200 --per-type 10
uv run --frozen python -m loopcheck.evaluate_agent --data data/synthetic/d3_dev  --out results/d3/dev
uv run --frozen python -m loopcheck.evaluate_agent --data data/synthetic/d3_test --out results/d3/test
```

**D2.1 evaluation** (default pipeline vs the no-mask baseline, fresh splits):

```bash
uv run --frozen python -m loopcheck.synth --out data/synthetic/d21_dev  --seed 3100 --per-type 8  --case-set d21
uv run --frozen python -m loopcheck.synth --out data/synthetic/d21_test --seed 3200 --per-type 10 --case-set d21
for s in dev test; do
  uv run --frozen python -m loopcheck.evaluate --data data/synthetic/d21_$s --out results/d21/${s}_masked
  uv run --frozen python -m loopcheck.evaluate --data data/synthetic/d21_$s --out results/d21/${s}_baseline \
      --config configs/baseline_no_marker_mask.json
  uv run --frozen python scripts/compare_runs.py --data data/synthetic/d21_$s \
      --a results/d21/${s}_baseline --b results/d21/${s}_masked \
      --config-a configs/baseline_no_marker_mask.json --out results/d21/${s}_comparison
done
```

**Reproducing the D2 results** (`results/eval_dev`, `results/eval_test`) needs the D2
case set and the no-mask config:

```bash
uv run --frozen python -m loopcheck.synth --out data/synthetic/test --seed 2000 --per-type 10 --case-set d2
uv run --frozen python -m loopcheck.evaluate --data data/synthetic/test --out results/eval_test \
    --config configs/baseline_no_marker_mask.json
```

The dev split is the same with `--seed 1000 --per-type 8`.

Generated images (`data/synthetic/`) are not committed; they regenerate byte-for-byte
from the seeds. Evaluation outputs are committed under `results/`. All reported
metrics are on **synthetic** data.

## Layout

```
loopcheck/      config, imageio, registration, markers, compare, pipeline, agent, cli, synth, evaluate, evaluate_agent,
                service, aws_lambda, local_server, client (D4)
app/            streamlit_app.py (D4 demo UI)
deploy/         build_lambda_zip.sh, preflight.sh (read-only), requirements-lambda.txt, Dockerfile (alternative), .env.example, samconfig.example.toml
template.yaml   SAM template: HTTP API + Lambda (not deployed)
tests/          pytest suite
scripts/        check_opencv5.py (D1), compare_runs.py (D2.1 before/after)
configs/        baseline_no_marker_mask.json
docs/           opencv5_verification.md (D1), d2_pipeline.md (D2 + D2.1), d3_agent.md (D3 controller, results, limitations)
results/        opencv5_check.json, eval_dev/ + eval_test/ (D2), d21/ (D2.1), d3/ (D3, incl. agent traces)
data/demo/      demo reference/inspection pair + ground truth
data/demo_cases/ 8 bundled synthetic UI cases (seed 5000) with ground truth and selection notes
```
