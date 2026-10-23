---
title: "LoopCheck: Agentic Visual Inspection with OpenCV 5"
subtitle: "Technical report · OpenCV AI Competition 2026"
---

**Live demo:** <https://loopcheck.streamlit.app> · **Source:** <https://github.com/arnavgupta2004/Loopcheck>

> **Read this first.** LoopCheck is a research prototype. Every metric in this report comes
> from **synthetic** images produced by the project's own tray generator; there is no
> real-world validation yet. Anomaly scores and thresholds are **uncalibrated heuristics,
> not probabilities**. No neural network was trained: the system is classical OpenCV 5
> computer vision driven by an **explicit, rule-based agent controller**.

# 1. Problem and intended users

Many assembly and kitting processes end with a visual check: does the tray, fixture or
board in front of the camera match the reference? Typical failures are a missing part, a
part in the wrong slot, a foreign object, or a change too small to be sure about. A
classifier that outputs a single label hides *why* it decided, and it forces an answer
even when the evidence is weak.

LoopCheck compares a **reference image** (what should be there) with an **inspection
image** (what is there). It returns one of three outcomes, with the evidence behind it:

| Outcome | Meaning |
|---|---|
| **APPROVE** | Reliable alignment, enough area inspected, and no unresolved evidence |
| **DEFECT** | At least one difference region confirmed by the controller's rules |
| **HUMAN_REVIEW** | Alignment unreliable, evidence conflicting or too weak, a blind spot unscreened, or any internal error |

**Intended users:**
- quality engineers and line operators, who need a *decision-support* tool that shows its
  evidence and escalates uncertain cases;
- researchers exploring transparent, tool-using inspection agents.

LoopCheck is **not** intended for unsupervised, safety-critical accept/reject decisions.

# 2. System architecture and data flow

![LoopCheck architecture as implemented (template.yaml, aws_lambda.py, service.py, agent.py, streamlit_app.py).](figures/architecture.png)

**Data flow for one inspection:**
1. In the Streamlit app (`app/streamlit_app.py`), the user picks a bundled synthetic case or
   uploads two PNG/JPEG images.
2. **HTTP API mode** (the hosted demo): the app's Python server sends both images as base64
   JSON to `POST /inspect` on Amazon API Gateway (HTTP API), using the standard-library
   client `loopcheck/client.py`. The browser never calls AWS directly. The endpoint is
   read from Streamlit Secrets and is never shown in the UI.
3. API Gateway invokes **AWS Lambda** (Python 3.13, x86_64), handler
   `loopcheck.aws_lambda.handler`. The handler:
   - validates request and image size limits (≤ 2 MB and ≤ 4096 px per image);
   - calls `loopcheck.service.analyze()`.
4. `analyze()` decodes the images and runs the **agent controller**
   (`loopcheck/agent.py`). The controller calls the OpenCV 5 tools one step at a time.
5. The JSON response carries:
   - the decision, the termination rule and the reasons;
   - the full step trace and the candidate regions with their evidence;
   - coverage, including areas that were **not** inspected;
   - three evidence images (aligned, anomaly heatmap, annotated overlay), as JPEG ≤ 1024 px.

   Lambda writes one metadata-only log line per request to CloudWatch Logs.
6. **Local (in-process) mode** runs the same `analyze()` inside the Streamlit process, which
   is useful offline and for development.

There is no ML model, database, queue or persistent image storage anywhere in the system.

# 3. OpenCV 5 implementation

The project pins `opencv-contrib-python-headless==5.0.0.93`; importing the package fails
if any other major version is loaded. `scripts/check_opencv5.py` exercises every OpenCV
API the pipeline uses against known ground truth: 9 of 9 checks pass on OpenCV 5.0.0
(`evidence/opencv5_check.json`).

## 3.1 Registration (`loopcheck/registration.py`)

The goal is a homography **H** that maps reference pixels to inspection pixels.
1. **ArUco first.** If both images share at least 3 marker ids, H comes from the matched
   corners (sub-pixel corner refinement).
2. **SIFT fallback.** Otherwise SIFT keypoints are matched with a ratio test, and H is
   estimated with **USAC_MAGSAC**.
3. **Quality gate.** Every H must pass all of these, or registration fails and no verdict
   is produced:
   - enough matches and inliers;
   - inlier ratio ≥ 0.30;
   - reprojection error ≤ 1.5 px (≤ 2.0 px for ArUco);
   - a plausible scale and orientation, and a convex frame projection;
   - at least 60 % of the reference visible in the inspection image.
4. **Alignment.** `warpPerspective(..., WARP_INVERSE_MAP)` brings the inspection image into
   the reference frame, and an eroded valid-overlap mask records which pixels can be
   compared.

On the frozen D2 test split, registration succeeded on every case that should register.
The median corner error was 0.064 px against the generator's true homography, and all
30 registration-failure cases were correctly rejected.

## 3.2 Image comparison (`loopcheck/compare.py`)

**Normalisation** brings the inspection image to the reference first:
- a robust global L gain/offset and a/b shift in Lab colour space;
- a local low-frequency illumination correction;
- **sharpness equalisation**: the sharper image is blurred until the variance-of-Laplacian
  focus measures match.

**Four cues** are then computed:

| Cue | What it measures | Default weight |
|---|---|---|
| intensity | misalignment-tolerant L difference | 0.30 |
| SSIM | 1 − local SSIM | 0.20 |
| gradient | tolerant Sobel-magnitude difference | 0.20 |
| colour | tolerant a/b distance | 0.30 |

"Tolerant" means a pixel counts only when it falls outside the other image's 3×3
min–max range. This suppresses sub-pixel edge residue but keeps real changes.

The cues are fused by a weighted mean into an anomaly map. The agent's
illumination-robust variant uses histogram matching and structure-weighted cues
(0.10 / 0.35 / 0.35 / 0.20).

## 3.3 Marker masking (`loopcheck/markers.py`)

ArUco fiducials sit at tonal extremes, where tone correction fails, and caused false
positives (D2 failure analysis). Every marker **actually detected** in either image is
excluded from scoring:
- the polygon is offset outward by 0.25 × side + 2 px, which covers the quiet zone;
- the exclusion is then grown by 6 px, so marker contrast can't leak through the blur
  and SSIM windows;
- both images are left untouched: no inpainting, no invented content.

Excluded pixels are reported as **NOT inspected**, never as clean. The agent screens them
separately (§4). Safety caps disable masking if a single marker or the total exclusion is
implausibly large. On the D2.1 synthetic test split, masking reduced marker-attributable
false-positive regions from 32 to 2.

## 3.4 Anomaly-region extraction

The fused map is smoothed (σ = 1.5), thresholded at 0.30, opened (3 px) and closed (9 px).
Connected components are kept if their area is at least 0.03 % of the valid area and
their peak score is at least 0.45.

Each region reports:
- its box in reference pixels (also projected into the inspection image);
- its peak and mean score, and the evidence from each cue;
- its dominant cue, and a heuristic change hint (removed / added / appearance changed).

The hint is descriptive, not a classification. Every threshold lives in
`loopcheck/config.py` and was fixed on a development split before testing.

# 4. Adaptive agent controller

`loopcheck/agent.py` implements a bounded **perception → decision → action** loop:

```
observe state ─▶ policy: ordered explicit rules pick ONE tool ─▶ execute tool
      ▲                                                               │
      └──── record observation, rule, parameters and effect ◀─────────┘
        ... until a termination rule fires: APPROVE / DEFECT / HUMAN_REVIEW
```

**Perception tools** are deterministic OpenCV 5 functions:
- `register(variant)`: default, sharpness-matched SIFT, relaxed-ratio SIFT;
- `compare(variant)`: default, illumination-robust, sensitive;
- `reinspect(candidate)`: crop views at 1×, 2× or 0.5×, after a local re-alignment, and
  with structural weights;
- `content_check()`: SIFT agreement outside the markers, to check it is the *same item*;
- `marker_band_check()`: an unmasked diagnostic over the excluded marker bands.

**The policy** is an ordered list of rules over the observations. Each executed step
records:
- the rule id, its rationale and the observations it used;
- the tool and its parameters;
- the output and its effect on the state.

The adaptive branches are:

| Branch | Observation that triggers it | Action |
|---|---|---|
| A | registration fails the quality gate | try two alternative registrations; all fail → HUMAN_REVIEW (A2) |
| B | measured global tone shift | add an illumination-robust comparison |
| C | candidate not strong (peak < 0.75, area < 400 px, or tone shift) | re-inspect: confirm only if the structural view (≥ 0.70) and ≥ 1 other view reproduce it |
| D | 1–3 isolated sub-threshold hotspots | sensitive scan; reproduction → HUMAN_REVIEW, never an automatic DEFECT |
| E | marker pixels were excluded | screen the band; unexplained finding → HUMAN_REVIEW (T3) |
| F | ArUco-only alignment or widespread change | content check; mismatch → HUMAN_REVIEW (F2) |
| Q1 | image too blurred to decide | HUMAN_REVIEW |

**Safety properties**, each covered by a test in `tests/test_agent.py`:
- at most 16 tool calls, and duplicate actions are refused;
- every tool exception, policy fault, input error or exhausted budget ends in
  **HUMAN_REVIEW**: no error path leads to APPROVE;
- **APPROVE** requires all of: reliable registration, at least 80 % of the frame inspected,
  no unresolved or ambiguous candidate, and no unexplained marker-band finding;
- a candidate is *dismissed* only when no view reproduces it **and** a concrete
  explanation exists. Otherwise it stays ambiguous and is escalated.

![The running app (Local backend capture) on bundled case 02 "Missing part": DEFECT, rule T1, with reference, inspection, annotated overlay, aligned image and anomaly heatmap.](figures/app_missing_part.jpg)

![Agent trace in the app for bundled case 05 "Ambiguous tiny object": six steps (R0 register, R1 compare, F1 content check, D1 sensitive compare, C1 re-inspect, E1 marker band) ending in HUMAN_REVIEW, rule T2.](figures/app_agent_trace.jpg)

# 5. Deployment on AWS

**Infrastructure** (`template.yaml`, AWS SAM, region ap-south-1):
- **API Gateway HTTP API:**
  - routes `GET /health` and `POST /inspect`;
  - default-route throttling at 2 requests/s, burst 5;
  - CORS restricted to a single origin.
- **Lambda function:**
  - Python 3.13 on x86_64, 29 s timeout;
  - deployed as a zip that bundles OpenCV 5.0.0.93 and NumPy 2.5.4;
  - its execution role holds only the AWS-managed basic logging policy;
  - concurrency mode is configurable (reserved or unreserved), and reserved concurrency 0
    is never used.
- **Log group:** 7-day retention; the handler logs request metadata only, never images,
  request bodies or credentials.

**Deployment hygiene:**
- **Packaging.** The Lambda zip is built from Linux manylinux wheels.
  `deploy/build_lambda_zip.sh` and `deploy/lambda_package.py` check it structurally:
  OpenCV 5 and NumPy present as Linux ELF x86-64 binaries, pinned versions, and the
  250 MiB unzipped limit. On Linux, the build also imports the unpacked zip and calls
  `/health`.
- **Deployer identity.** Routine deployment uses a least-privilege IAM identity, scoped to
  the `loopcheck-demo` stack, not the root user. Its policy is in `deploy/iam/` and
  checked by offline tests.
- **Preflight.** `deploy/preflight.sh` runs read-only checks before deploying.

**Smoke-test evidence: what is included and what is not.**
- **Reported by the team (deployed system).** The stack was deployed and the API smoke
  test (`python -m loopcheck.client --url <ApiUrl> --expect DEFECT`) completed
  successfully. The public demo uses that API in HTTP API mode. The verbatim terminal
  output from the deployed run is *not* stored in the repository, and endpoints are
  deliberately not published, so this package does not reproduce it.
- **Verifiable in this package.** `evidence/smoke_test_local_handler.txt` is the output of
  the same smoke-test client run against `loopcheck.local_server`, which serves the
  identical Lambda handler on localhost. It shows:
  - health `ok`, OpenCV `5.0.0` at health and at inspect;
  - decision `DEFECT`, rule `T1`;
  - tool sequence `R0:register → R1:compare`;
  - evidence images returned;
  - `SMOKE TEST: PASS`.
- **Not measured.** Deployed latency, cold-start times and cost were not recorded, so none
  are reported.

# 6. Synthetic evaluation

**Methodology:**
- **Data.** A procedural generator (`loopcheck/synth.py`) renders 800×600 trays of coloured
  parts, with or without ArUco markers. It applies controlled perturbations: lighting
  (gain, offset, gamma, colour cast, gradient), perspective, blur, noise, JPEG
  compression. It also injects defects:
  - a missing part, a shifted part, a foreign object;
  - hard missing parts under strong lighting;
  - subtle 7–10 px foreign objects;
  - a missing part under a gamma shift.

  Registration-failure cases (unrelated scene, low overlap, heavy blur) and different
  trays carrying the same markers are included too.
- **Protocol.** All thresholds were tuned on a development split (seed 4100, 144 cases).
  The configuration was frozen and committed. The held-out test split (seed 4200, 180
  cases, 18 types × 10) was then generated and evaluated **once**.
- **Expected outcome per category:**
  - good images → APPROVE;
  - defects → DEFECT with a box at IoU ≥ 0.3;
  - registration failures and different trays → HUMAN_REVIEW;
  - for good images and defects, HUMAN_REVIEW counts as a safe but non-automatic outcome.
- **Baseline.** The "one-shot" pipeline (register + compare, mapped mechanically) is
  evaluated on the same cases.

![Held-out synthetic results, read from results/d3/test/metrics.json. Synthetic evaluation, not real-world validation.](figures/evaluation_results.png)

| Metric (test split, n = 180, synthetic) | One-shot | Agent |
|---|---|---|
| Image-level precision / recall / F1 (DEFECT = positive) | 0.865 / 0.963 / 0.911 | 1.000 / 0.887 / 0.940 |
| False-positive rate on good images | 0.200 | 0.000 |
| Good images not auto-approved | 0.200 | 0.267 |
| Automatic decision coverage | 0.833 | 0.639 |
| Accuracy of automatic decisions | 0.833 | 1.000 |
| Errors among automatic decisions | 25 | 0 |
| Defects approved (critical errors) | 3 | 0 |
| HUMAN_REVIEW rate | 0.167 | 0.361 |
| Task success | 0.861 | 0.889 |
| Latency mean / median / p90 (4-core CPU, 800×600) | 0.37 / 0.33 / 0.54 s | 0.67 / 0.57 / 1.02 s |

**The trade-off.** The agent buys reliability by abstaining:
- **Gains:**
  - among the decisions it makes automatically, none was wrong on this split, against 25
    for one-shot;
  - it approved no defect, against 3.
- **Costs:**
  - automatic coverage drops from 83 % to 64 %, and 36 % of cases go to a person;
  - DEFECT recall drops from 0.963 to 0.887; every missed defect was escalated, none
    approved;
  - 7 of 10 good images under lighting changes were escalated, mostly through the
    sensitive scan (branch D);
  - latency roughly doubles.

About 22 % of all test cases are *expected* HUMAN_REVIEW (registration failures and
different trays). "Zero automatic errors" holds for this synthetic split only: on the
development split the agent made one automatic error, which approved one subtle defect.

# 7. Limitations, failure cases and responsible use

**Limitations:**
- **Synthetic only.** One generator produced every split. Real photographs bring different
  noise, specularities, parallax and materials. Real-world accuracy is unknown.
- **Uncalibrated scores.** Peaks and thresholds rank evidence; they are not probabilities.
  Two thresholds sit in narrow development-split gaps: confirmation 0.70 and band 0.60.
- **Review rate.** 36 % HUMAN_REVIEW on the test split, driven mainly by lighting changes
  on good images.
- **Sensitivity floor.** Defects below the sensitive scan's floor, or among more than 3
  hotspots, can be missed.
- **Fixture assumptions.** Registration assumes a mostly planar scene; the content check
  needs texture outside the markers.

**Known failure cases:**
- gamma-shifted good trays escalated;
- three real defects whose structural evidence fell below the confirmation peak, which
  were escalated instead of reported;
- different trays carrying the same markers. These are escalated correctly via F2, but
  only because a content check exists.

**Responsible use:**
- LoopCheck is not certified for production or safety-critical use.
- HUMAN_REVIEW is a designed outcome, and a person must act on it.
- Before any operational use, an organisation would need a labelled real-image
  evaluation on its own parts, cameras and lighting.

**Privacy and security:**
- Images are processed in memory and returned to the requesting client.
- Nothing is stored server-side; Lambda logs contain metadata only.
- The demo API is public and unauthenticated (throttled, with CORS limited to one origin;
  CORS is not authentication), so do not upload sensitive or proprietary images to the
  public demo.
- No credentials, account identifiers or endpoints are included in this package.

# 8. Future work

1. Collect and label real tray photographs; evaluate on a held-out real set and report
   per-condition results.
2. Reduce lighting-driven escalations: require structural reproduction before a sensitive
   candidate escalates. Measure this on a fresh split.
3. Ablate branch B (illumination-robust comparison) and calibrate the confirmation and band
   thresholds with more data.
4. A monotone tone-curve model for extreme tones, to replace part of the marker masking.
5. Authentication and per-client quotas for the API; a review queue that stores escalated
   cases with their traces (with consent and retention controls).

# Appendix: provenance

| Claim | Source in the repository |
|---|---|
| Pipeline design and D2/D2.1 results | `docs/d2_pipeline.md`, `results/eval_test/`, `results/d21/` |
| Agent rules, branches and D3 results | `docs/d3_agent.md`, `results/d3/test/metrics.json` (copied to `evidence/`) |
| OpenCV 5 verification | `docs/opencv5_verification.md`, `results/opencv5_check.json` (copied to `evidence/`) |
| AWS architecture | `template.yaml`, `loopcheck/aws_lambda.py`, `docs/d4_demo_and_deployment.md` |
| Tests | `tests/`: 262 passing at the time of packaging (`uv run --frozen --group demo pytest -q`) |
