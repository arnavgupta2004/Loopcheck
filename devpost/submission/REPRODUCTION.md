---
title: "LoopCheck: reproduction and testing guide"
---

- **Repository:** <https://github.com/arnavgupta2004/Loopcheck>
- **Live demo:** <https://loopcheck.streamlit.app>

## 1. Try the live demo (no installation)

1. Open <https://loopcheck.streamlit.app>. It is the public frontend, configured to call the
   deployed AWS backend (stack `loopcheck-demo`, `ap-south-1`) in HTTP API mode. If the app
   has been idle it may need about a minute to wake up.
2. In the sidebar, under **Backend**, choose **HTTP API**. This runs the inspection on the
   AWS Lambda backend. Press **Check API health**; it should report `ok: OpenCV 5.0.0`.
   (**Local (in-process)** runs the same code inside the app instead.)
3. Under **Input**, keep **Bundled synthetic case**, pick a case, and press **Run inspection
   agent**.
4. Read the decision banner (`DECISION · rule · backend · time`), then the tabs:
   - **Images:** reference, inspection, annotated overlay, aligned image and heatmap;
   - **Detected regions:** candidate regions with their scores;
   - **Agent trace:** each step with its rule, rationale, observations, tool and effect;
   - **Registration & coverage**, **Limitations**, and **Raw JSON & downloads**.

**Bundled cases.** All are synthetic.
- The decisions below were re-verified **locally** when this package was built, with
  `loopcheck.service.analyze` (the same code the Lambda runs).
- They were not individually re-run against the deployed API. The deployed smoke test
  covered case 02 "Missing part": `DEFECT`, rule T1.
- Results through the live app should match, but this table is local evidence.

| Case | Decision | Rule | What it shows |
|---|---|---|---|
| Clean (identical) | APPROVE | T5 | Approval needs reliable registration, ≥ 80 % inspected, no unresolved evidence |
| Missing part | DEFECT | T1 | Strong first-pass evidence confirmed directly (rule C0) |
| Shifted part | DEFECT | T1 | Part moved to another position |
| Lighting change (good tray) | APPROVE | T5 | Illumination-robust comparison (branch B) runs, then approval |
| Ambiguous tiny object | HUMAN_REVIEW | T2 | Weak evidence is re-inspected and escalated, not guessed |
| Defect next to an ArUco marker | DEFECT | T1 | Marker masking, illumination-robust compare and re-inspection |
| Different tray, same markers | HUMAN_REVIEW | F2 | The content check catches a different item that has the same fiducials |
| Registration failure | HUMAN_REVIEW | A2 | Three registration attempts fail, so there is no verdict |

**Upload.** You can also upload your own PNG/JPEG pair (≤ 2 MB and ≤ 4096 px each). The
public demo is unauthenticated, so don't upload sensitive images.

## 2. Run locally

**Requirements:** Linux or macOS, Python 3.12+, [uv](https://docs.astral.sh/uv/) and git.

```bash
git clone https://github.com/arnavgupta2004/Loopcheck.git
cd Loopcheck
uv sync --frozen --group demo --group dev     # OpenCV 5.0.0.93, NumPy, Streamlit, pytest
uv run --frozen python scripts/check_opencv5.py              # expect: SUMMARY: 9/9 checks passed on OpenCV 5.0.0
uv run --frozen --group demo streamlit run app/streamlit_app.py   # open http://localhost:8501
```

**Command line** (agent mode writes `report.json` with the full trace, and `overlay.png`):

```bash
uv run --frozen python -m loopcheck.cli --reference data/demo_cases/02_missing_part/reference.png \
  --inspection data/demo_cases/02_missing_part/inspection.png --output-dir outputs/missing --agent
```

**The same request path as Lambda, locally:**

```bash
uv run --frozen python -m loopcheck.local_server --port 8000 &          # serves loopcheck.aws_lambda.handler
uv run --frozen python -m loopcheck.client --url http://127.0.0.1:8000 \
  --case data/demo_cases/02_missing_part --expect DEFECT                # ends with: SMOKE TEST: PASS
```

## 3. Tests

```bash
uv run --frozen --group demo --group dev pytest -q
```

At packaging time this ran **262 tests, all passing**, in about 60 s on a 4-core CPU. They
cover:
- registration conventions, comparison cues and marker masking;
- every agent branch and safety property (no error path leads to APPROVE);
- the service and Lambda handler, including metadata-only logging;
- the Streamlit UI (headless), including Secrets handling;
- deployment packaging, preflight and the IAM policy (all offline: no AWS calls).

## 4. Reproduce the evaluation (synthetic)

The committed results are in `results/`. To regenerate the D3 test split (seed 4200) and
re-evaluate the frozen configuration:

```bash
uv run --frozen python -m loopcheck.synth --out data/synthetic/d3_test --seed 4200 --per-type 10 --case-set d3
uv run --frozen python -m loopcheck.evaluate_agent --data data/synthetic/d3_test --out results/d3/test
```

`docs/d3_agent.md` documents the exact commands, splits and protocol. Generated datasets
are not committed. All results are on **synthetic** data, and scores are **uncalibrated
heuristics, not probabilities**.

## 5. Deploy your own backend (optional)

The runbook is `docs/d4_demo_and_deployment.md`. The steps are:
1. Build and verify the Lambda zip: `bash deploy/build_lambda_zip.sh`.
2. Run the read-only preflight: `bash deploy/preflight.sh`.
3. Deploy with `sam deploy` (stack `loopcheck-demo`).
4. Run the smoke test above against your `ApiUrl`.

For routine deployment, use the least-privilege identity described in
`docs/d4_2_deployment_identity.md`, not the root user.
