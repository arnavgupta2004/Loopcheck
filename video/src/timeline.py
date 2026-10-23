"""Single source of truth for the LoopCheck demo film: scenes, narration, cues.

Times are in seconds. The renderer, the audio stems, `voiceover_timing.csv` and
`voiceover_script.md` are all generated from this file, so retiming after the real voice-over
is recorded means editing the numbers here and re-rendering (see video/README.md).
"""

from __future__ import annotations

FPS = 30
WIDTH, HEIGHT = 1920, 1080
DURATION = 88.0

# id, start, end, label, narration window (vo_start, vo_end), narration, visual cue
SCENES = [
    dict(id="S1", start=0.0, end=8.0, label="The inspection problem", vo=(0.5, 7.6),
         text="A missing part may be a few dozen pixels. Inspection asks: "
              "does what the camera sees match what should be there?",
         cue="Macro push-in on the real synthetic tray (demo case 02); blue scan sweep; red bracket on the empty slot "
             "(ground-truth box mapped through the estimated homography). Title: Small defects. High-stakes decisions."),
    dict(id="S2", start=8.0, end=20.0, label="Product reveal", vo=(8.7, 18.9),
         text="LoopCheck is a research prototype for agentic visual inspection. "
              "Rather than one opaque label, it returns a decision and its evidence.",
         cue="Wordmark reveal; reference vs inspection; divider wipe over the registered (aligned) inspection; "
             "statement: decision + rule + tool trace."),
    dict(id="S3", start=20.0, end=34.0, label="The agent workflow", vo=(20.4, 33.4),
         text="Its agent is a rule-based controller, not a neural network. It calls OpenCV tools in turn: "
              "register the images, then compare them into an anomaly map. "
              "Extra checks run only when the evidence calls for them.",
         cue="Flow: inputs -> controller -> R0 register -> R1 compare -> evidence + decision. Real homography warp, "
             "real heatmap and candidate (peak 0.94, 1831 px), C0 confirm, T1 DEFECT; conditional tools strip."),
    dict(id="S4", start=34.0, end=53.0, label="Real application", vo=(35.2, 52.2),
         text="Here is the running app. An unchanged tray is approved. A missing component is a confirmed defect. "
              "A faint, tiny object can't be confirmed, so the agent escalates to human review instead of guessing.",
         cue="Real Streamlit screenshots (local mode): Clean -> APPROVE T5; Missing part -> DEFECT T1 with overlay close-up; "
             "Ambiguous tiny object -> HUMAN_REVIEW T2 with the agent trace."),
    dict(id="S5", start=53.0, end=67.0, label="Evidence and engineering", vo=(53.3, 66.6),
         text="Strong evidence is confirmed directly; weaker regions must be reproduced by independent views, or go to a person. "
              "The same controller runs on AWS Lambda behind API Gateway, returning the decision, its rule and the tool trace.",
         cue="Candidate close-up (case 05) with rules C0 / C1 / T2; architecture: Streamlit client -> API Gateway (HTTP API, "
             "throttled) -> Lambda (Python 3.13, OpenCV 5) -> controller + OpenCV tools -> JSON; terminal with the real smoke-test output."),
    dict(id="S6", start=67.0, end=80.0, label="Transparency and limitations", vo=(67.5, 79.3),
         text="On a held-out synthetic test set, it made no automatic errors and sent about a third of cases to review. "
              "That's synthetic validation, not real-world performance, and the scores aren't probabilities.",
         cue="Artefacts returned by every run; held-out synthetic metrics (180 cases, 0 automatic errors, 36% human review); "
             "qualification: Research prototype • Synthetic-data evaluation."),
    dict(id="S7", start=80.0, end=88.0, label="Closing", vo=(81.0, 85.4),
         text="Visual evidence. Explicit decisions. Inspectable execution.",
         cue="Return to the macro tray; LoopCheck wordmark and closing line; hold; fade to black by 88.0 s."),
]

# Sound effects (original, synthesised in audio.py): (time s, kind, gain dB)
SFX = [
    (0.0, "swell", -20), (2.2, "scan", -14), (4.0, "ping", -22), (7.45, "whoosh", -21),
    (8.55, "hit", -18), (12.6, "swish", -24), (16.7, "whoosh_soft", -25), (19.55, "whoosh", -21),
    (22.4, "tick", -22), (25.6, "lock", -20), (26.4, "tick", -22), (28.0, "ping_low", -23), (30.2, "hit_soft", -20),
    (33.6, "whoosh", -21),
    (35.2, "click", -18), (35.55, "hit_soft", -22), (40.05, "click", -19), (40.45, "click", -18), (40.8, "hit_soft", -22),
    (46.05, "click", -19), (46.45, "click", -18), (46.8, "hit_soft", -22), (49.4, "swish", -26),
    (52.7, "whoosh", -21), (55.2, "ping_low", -24), (58.45, "whoosh_soft", -23), (62.0, "tick", -26), (62.25, "tick", -28),
    (66.7, "whoosh", -21), (72.4, "swish", -24), (75.6, "hit_soft", -22),
    (79.7, "whoosh", -21), (81.0, "hit", -19),
]

# Intended music cues (the pad in audio.py follows them; replace with a licensed track if preferred)
MUSIC_CUES = [
    (0.0, "Low sustained pad fades in; sparse, unresolved (D minor colour)."),
    (8.0, "Lift into the reveal; add the upper voice."),
    (20.0, "Steady, quieter bed under the workflow explanation."),
    (34.0, "Slight lift for the live app section; keep under -24 dBFS for voice-over."),
    (53.0, "Hold steady under the architecture and terminal."),
    (67.0, "Thin out for the limitations; nothing celebratory."),
    (80.0, "Resolve to the tonic for the wordmark; fade to silence by 88.0 s."),
]


def words(text: str) -> int:
    return len(text.split())


def total_words() -> int:
    return sum(words(s["text"]) for s in SCENES)
