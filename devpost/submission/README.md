# LoopCheck: supplementary submission materials

OpenCV AI Competition 2026 · agentic visual inspection with OpenCV 5.

- **Live demo:** <https://loopcheck.streamlit.app>
- **Source code:** <https://github.com/arnavgupta2004/Loopcheck>

## Contents

| File | What it is |
|---|---|
| `technical_report.pdf` | The technical report (also as `technical_report.md`). Start here |
| `REPRODUCTION.md` | How to try the demo, run locally, run the tests and reproduce the evaluation |
| `figures/architecture.png` | The architecture as implemented: Streamlit → API Gateway → Lambda → agent controller + OpenCV 5 tools → JSON response |
| `figures/evaluation_results.png` | One-shot pipeline vs adaptive agent on the held-out **synthetic** test split |
| `figures/app_missing_part.jpg` | App screenshot, **local-backend run**: bundled case "Missing part" → DEFECT (T1) |
| `figures/app_agent_trace.jpg` | App screenshot, **local-backend run**: agent trace for "Ambiguous tiny object" → HUMAN_REVIEW (T2) |
| `evidence/metrics_d3_test.json` | Committed evaluation results (`results/d3/test/metrics.json`), the source of every metric shown |
| `evidence/opencv5_check.json` | OpenCV 5.0.0 API verification (9/9 checks) |
| `evidence/smoke_test_local_handler.txt` | Smoke-test output of `loopcheck.client` against the Lambda handler served **locally** (the deployed result is summarised in the report, §5) |
| `evidence/health_local.json` | `GET /health` response from the same handler |
| `evidence/bundled_cases_verification.json` | Decisions of all 8 bundled cases, re-run **locally** when this package was built |

## Important context

- **Synthetic data only.** All evaluation uses synthetic images, and no real-world
  validation has been done. Anomaly scores are uncalibrated heuristics, **not
  probabilities**.
- **No trained model.** No neural network was trained. LoopCheck combines classical
  OpenCV 5 algorithms (ArUco/SIFT registration with USAC-MAGSAC, multi-cue comparison,
  marker masking, connected-component region extraction) with an explicit, rule-based
  agent controller.
- **Deployment.** The backend is deployed: stack `loopcheck-demo` in AWS `ap-south-1`.
  - The deployed API smoke test passed: health `ok`; OpenCV 5.0.0 in both the health and
    inspection responses; `DEFECT`, rule T1, tools R0 → R1; about 1.55 s server-side and
    4.30 s end-to-end for that one request.
  - The public frontend <https://loopcheck.streamlit.app> uses it in HTTP API mode and was
    validated manually.
  - The smoke test shows one successful request. It is not an accuracy measurement and not
    a latency, cold-start or memory benchmark; none of those has been measured. The endpoint
    and the raw terminal output are not included.
- **What the screenshots show.** They are real captures of the Streamlit app from
  **local-backend** (in-process) runs, not from the live deployed API.
- **Excluded from this package:** credentials, account identifiers, endpoints, IAM user
  details, the Lambda deployment zip, the source tree and generated datasets. All code is
  in the repository.
