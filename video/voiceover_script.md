# LoopCheck demo film: voice-over script

Total: **186 words** across 77 s of narration windows in an 88 s film (≈145 words per minute inside the windows). Generated from `video/src/timeline.py`; edit the text and timings there, then re-render.

## Delivery notes

- Calm, precise, unhurried. Technical documentary, not an advert. No hype words.
- Pronunciation: *LoopCheck* (one word, stress on Loop); *OpenCV* = "open C V"; *HUMAN_REVIEW* is read "human review".
- `/` marks a short breath; `//` a deliberate pause (≈0.6 s) for a visual beat.
- Keep the qualification in scene S6 audible and unrushed: it is a required part of the claim.
- Record 48 kHz / 24-bit mono or stereo WAV, about 1 s of room tone at the head, then start at 00:00.5.

## Script

### S1 · The inspection problem · 00:00.00–00:08.00 (speak 00:00.50–00:07.60, 21 words)

> A missing part may be a few dozen pixels. // Inspection asks: / does what the camera sees / match what should be there?

*On screen:* Macro push-in on the real synthetic tray (demo case 02); blue scan sweep; red bracket on the empty slot (ground-truth box mapped through the estimated homography). Title: Small defects. High-stakes decisions.

### S2 · Product reveal · 00:08.00–00:20.00 (speak 00:08.70–00:18.90, 21 words)

> LoopCheck is a research prototype for agentic visual inspection. // Rather than one opaque label, / it returns a decision / and its evidence.

*On screen:* Wordmark reveal; reference vs inspection; divider wipe over the registered (aligned) inspection; statement: decision + rule + tool trace.

### S3 · The agent workflow · 00:20.00–00:34.00 (speak 00:20.40–00:33.40, 36 words)

> Its agent is a rule-based controller, / not a neural network. // It calls OpenCV tools in turn: / register the images, // then compare them into an anomaly map. // Extra checks run only when the evidence calls for them.

*On screen:* Flow: inputs -> controller -> R0 register -> R1 compare -> evidence + decision. Real homography warp, real heatmap and candidate (peak 0.94, 1831 px), C0 confirm, T1 DEFECT; conditional tools strip.

### S4 · Real application · 00:34.00–00:53.00 (speak 00:35.20–00:52.20, 34 words)

> Here is the running app. // An unchanged tray is approved. // A missing component is a confirmed defect. // A faint, tiny object can't be confirmed, / so the agent escalates to human review / instead of guessing.

*On screen:* Real Streamlit screenshots (local mode): Clean -> APPROVE T5; Missing part -> DEFECT T1 with overlay close-up; Ambiguous tiny object -> HUMAN_REVIEW T2 with the agent trace.

### S5 · Evidence and engineering · 00:53.00–01:07.00 (speak 00:53.30–01:06.60, 37 words)

> Strong evidence is confirmed directly; / weaker regions must be reproduced by independent views, / or go to a person. // The same controller runs on AWS Lambda behind API Gateway, / returning the decision, its rule / and the tool trace.

*On screen:* Candidate close-up (case 05) with rules C0 / C1 / T2; architecture: Streamlit client -> API Gateway (HTTP API, throttled) -> Lambda (Python 3.13, OpenCV 5) -> controller + OpenCV tools -> JSON; terminal with the real smoke-test output.

### S6 · Transparency and limitations · 01:07.00–01:20.00 (speak 01:07.50–01:19.30, 31 words)

> On a held-out synthetic test set, / it made no automatic errors / and sent about a third of cases to review. // That's synthetic validation, / not real-world performance, / and the scores aren't probabilities.

*On screen:* Artefacts returned by every run; held-out synthetic metrics (180 cases, 0 automatic errors, 36% human review); qualification: Research prototype • Synthetic-data evaluation.

### S7 · Closing · 01:20.00–01:28.00 (speak 01:21.00–01:25.40, 6 words)

> Visual evidence. // Explicit decisions. // Inspectable execution.

*On screen:* Return to the macro tray; LoopCheck wordmark and closing line; hold; fade to black by 88.0 s.

## Accuracy notes (what each claim rests on)

- *Rule-based controller, not a neural network*: `loopcheck/agent.py` (ordered policy rules R0, R1, A–G, C0/C1, T1–T5, X1–X4).
- *Register / compare*: ArUco or SIFT + USAC-MAGSAC homography (`registration.py`); fused intensity, SSIM, gradient and colour cues (`compare.py`).
- *Strong evidence confirmed directly*: rule C0 (peak ≥ 0.75, area ≥ 400 px, no illumination shift); weaker candidates need C1 re-inspection (structure view ≥ 0.70 and ≥ 1 other view).
- *Approved / defect / human review*: bundled cases 01 (APPROVE T5), 02 (DEFECT T1), 05 (HUMAN_REVIEW T2), re-run and read back from the app at capture time.
- *Held-out synthetic test set*: `results/d3/test/metrics.json`: 180 cases, 0 errors among automatic decisions, HUMAN_REVIEW rate 0.361.
- *AWS Lambda behind API Gateway*: `template.yaml` (HTTP API → Lambda python3.13, OpenCV 5). The on-screen terminal shows the smoke test against the same handler served locally unless `video/assets/api/smoke_deployed.txt` is provided (see README).
