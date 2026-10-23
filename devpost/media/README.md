# LoopCheck: Devpost gallery media

All images are 1800 × 1200 (3:2) PNG, each under 5 MB.

## Status

| # | File | Kind | Source | Status |
|---|---|---|---|---|
| 1 | `01-loopcheck-hero.png` | Genuine screenshot | Live app, HTTP API mode, case "Missing part" → DEFECT T1, Images tab | **Not captured yet** (see blocker) |
| 2 | `02-visual-evidence.png` | Genuine screenshot | Same run: reference, inspection, annotated overlay, aligned and anomaly heatmap | **Not captured yet** |
| 3 | `03-agent-trace.png` | Genuine screenshot | Live app, case "Defect next to an ArUco marker" → DEFECT T1. Agent trace R0 → R1 → F1 → B1 → C1 → E1, with the C1 re-inspection expanded and the final decision | **Not captured yet** |
| 4 | `04-human-review.png` | Genuine screenshot | Live app, case "Ambiguous tiny object" → HUMAN_REVIEW T2. Detected regions tab: an ambiguous candidate with its re-inspection views | **Not captured yet** |
| 5 | `05-loopcheck-architecture.png` | Generated graphic | `devpost/src/make_graphics.py`, from `template.yaml`, `aws_lambda.py`, `service.py`, `agent.py` and `streamlit_app.py` | Done |
| 6 | `06-evaluation-results.png` | Generated graphic | `devpost/src/make_graphics.py`, reading `results/d3/test/metrics.json` at render time | Done |

`contact-sheet.png` previews all six. It shows a red **NOT CAPTURED** tile for any image that
doesn't exist yet; regenerate it after capturing.

**Recommended gallery order:** 01 (hero, Devpost's cover image) → 02 → 03 → 04 → 05 → 06.

## Blocker: the public app was unreachable from the build environment

`https://loopcheck.streamlit.app` could not be opened from the environment that produced these
files: its network policy refuses that host (proxy `CONNECT` → HTTP 403), while other hosts
such as PyPI worked. So screenshots 01–04 were **not** captured, and no local or simulated
screenshot has been put in their place.

**To capture them**, from a machine that can reach the app, or after allowing
`loopcheck.streamlit.app` in this environment's network settings:

```bash
uv run --frozen --group demo --with playwright==1.56.0 python devpost/src/capture_live.py
uv run --frozen python devpost/src/contact_sheet.py
```

If you don't have the bundled Chromium, run `playwright install chromium` once first.

## What `capture_live.py` guarantees

- **Real backend only.** It selects **HTTP API**, presses **Check API health**, and requires
  `ok: OpenCV 5.x` before anything else.
- **Verified decisions.** For each case it runs the inspection and reads the decision banner
  back. It aborts unless the banner shows the expected decision **and** "HTTP API": MISSING
  → DEFECT T1, MARKER-ADJACENT DEFECT → DEFECT T1, AMBIGUOUS → HUMAN_REVIEW T2.
- **No endpoint leaks.** It aborts if the page contains an API Gateway or Lambda hostname, or
  an editable API URL field. (The app hides an endpoint configured in Streamlit Secrets.)
- **All or nothing.** Files are written only after every check has passed. Each run also
  writes `capture_log.json`, recording the timestamps, banner texts and crop boxes.
- **Honest pixels.** Screenshots are taken at device scale 1. The only edits are:
  - choosing a 1800 × 1200 window of a 1800-wide page;
  - hiding Streamlit Cloud's own viewer badge, which sits outside the app.

  There are no overlays and nothing is combined from different runs.

**Dry run.** The script was run end to end against the same app running locally in HTTP API
mode, with the real Lambda handler served by `loopcheck.local_server`. All four checks passed
(DEFECT T1, DEFECT T1, HUMAN_REVIEW T2, all "· HTTP API"). Those images were kept outside this
folder and are **not** deliverables.

## Generated graphics

- **05 architecture.** It shows only components that exist:
  - Streamlit Community Cloud (server-side `urllib` client; endpoint from Secrets, never shown);
  - API Gateway HTTP API (GET /health, POST /inspect; 2 req/s, burst 5; one CORS origin);
  - Lambda (Python 3.13, x86_64, OpenCV 5.0.0, 29 s timeout);
  - CloudWatch Logs (metadata only, 7 days).

  Inside the Lambda, the *agent controller* (decides) is drawn apart from the *OpenCV 5
  perception tools* (measure): register, marker masking, compare, reinspect and content_check.
  It ends with the structured JSON response. There is no ML model, database or queue.
- **06 evaluation.** Every number is read from `results/d3/test/metrics.json`: 180 held-out
  synthetic cases, seed 4200. All the figures in the brief match the committed data:
  - F1: 0.911 → 0.940;
  - false-positive rate on good images: 0.200 → 0.000;
  - automatic accuracy / coverage: 0.833 / 0.833 → 1.000 / 0.639;
  - HUMAN_REVIEW: 0.167 → 0.361;
  - task success: 0.861 → 0.889.

  The graphic also shows the costs:
  - DEFECT recall falls from 0.963 to 0.887 (the missed defects were escalated, not approved);
  - 26.7% of good images are not auto-approved, versus 20.0%;
  - mean latency rises from 0.37 s to 0.67 s.

  It notes that the agent made 1 automatic error on the development split, and is labelled
  "Synthetic held-out evaluation — not real-world validation".

Regenerate both graphics with `uv run --frozen python devpost/src/make_graphics.py`. Fonts are
Inter and JetBrains Mono (OFL / Apache 2.0, vendored in `video/assets/fonts`).
