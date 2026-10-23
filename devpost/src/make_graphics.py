"""Devpost graphics for LoopCheck: architecture (05) and evaluation (06). Reproducible, offline.

    uv run --frozen python devpost/src/make_graphics.py

Writes devpost/media/05-loopcheck-architecture.png and 06-evaluation-results.png at
1800x1200 (3:2). Everything is drawn at 2x and downsampled for clean anti-aliasing.
Metric values are READ from results/d3/test/metrics.json at render time (never typed in);
the architecture text mirrors template.yaml, loopcheck/aws_lambda.py, loopcheck/service.py,
loopcheck/agent.py and app/streamlit_app.py. Fonts: Inter / JetBrains Mono (OFL, vendored
in video/assets/fonts).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "devpost" / "media"
FONTS = ROOT / "video" / "assets" / "fonts"
W, H, S = 1800, 1200, 2  # output size and supersampling factor

BG = (11, 12, 14)
PANEL = (19, 21, 25)
LINE = (52, 57, 66)
WHITE = (242, 243, 245)
TEXT = (210, 214, 220)
MUTED = (140, 146, 156)
FAINT = (96, 101, 110)
RED = (229, 72, 77)
BLUE = (88, 166, 222)
BLUE_FILL = (20, 36, 50)
GREY_BAR = (120, 126, 136)
DECISION = {"APPROVE": (27, 127, 59), "DEFECT": (198, 40, 40), "HUMAN_REVIEW": (224, 123, 0)}  # app colours

FONT_FILES = {"display": "InterDisplay-SemiBold.otf", "regular": "Inter-Regular.otf", "medium": "Inter-Medium.otf",
              "semibold": "Inter-SemiBold.otf", "mono": "JetBrainsMono-Regular.ttf", "mono_medium": "JetBrainsMono-Medium.ttf"}
_font_cache: dict = {}


def font(kind: str, size: float) -> ImageFont.FreeTypeFont:
    key = (kind, int(round(size * S)))
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONTS / FONT_FILES[kind]), key[1])
    return _font_cache[key]


class Canvas:
    """Drawing in output pixels; rendered at S x and downsampled on save."""

    def __init__(self):
        self.img = Image.new("RGB", (W * S, H * S), BG)
        self.d = ImageDraw.Draw(self.img)
        g = ImageDraw.Draw(self.img)
        for x in range(0, W, 60):  # faint technical grid
            g.line([(x * S, 0), (x * S, H * S)], fill=(17, 18, 21), width=1)
        for y in range(0, H, 60):
            g.line([(0, y * S), (W * S, y * S)], fill=(17, 18, 21), width=1)

    def text(self, xy, s, kind="regular", size=20, fill=TEXT, anchor="la", tracking=0.0):
        x, y = xy
        f = font(kind, size)
        if not tracking:
            self.d.text((x * S, y * S), s, font=f, fill=fill, anchor=anchor)
            return f.getlength(s) / S
        total = sum(f.getlength(c) for c in s) / S + tracking * (len(s) - 1)
        cx = x - (total if anchor[0] == "r" else total / 2 if anchor[0] == "m" else 0)
        for c in s:
            self.d.text((cx * S, y * S), c, font=f, fill=fill, anchor="l" + anchor[1])
            cx += f.getlength(c) / S + tracking
        return total

    def label(self, xy, s, fill=MUTED, size=14, anchor="la"):
        return self.text(xy, s.upper(), "mono_medium", size, fill, anchor, tracking=1.6)

    def width(self, s, kind, size):
        return font(kind, size).getlength(s) / S

    def wrap(self, xy, s, max_w, kind="regular", size=18, fill=TEXT, leading=1.4):
        x, y = xy
        line = ""
        for word in s.split():
            trial = (line + " " + word).strip()
            if self.width(trial, kind, size) > max_w and line:
                self.text((x, y), line, kind, size, fill)
                y += size * leading
                line = word
            else:
                line = trial
        if line:
            self.text((x, y), line, kind, size, fill)
            y += size * leading
        return y

    def box(self, x, y, w, h, outline=LINE, fill=PANEL, width=1.6, radius=12, dash=False):
        r = [x * S, y * S, (x + w) * S, (y + h) * S]
        if dash:
            self.d.rounded_rectangle(r, radius * S, fill=fill)
            self._dashed_rect(x, y, w, h, outline, width)
        else:
            self.d.rounded_rectangle(r, radius * S, fill=fill, outline=outline, width=max(1, int(width * S)))

    def _dashed_rect(self, x, y, w, h, col, width):
        for (x0, y0, x1, y1) in ((x, y, x + w, y), (x + w, y, x + w, y + h), (x + w, y + h, x, y + h), (x, y + h, x, y)):
            n = int(math.hypot(x1 - x0, y1 - y0) // 10)
            for i in range(0, n, 2):
                a, b = i / n, min((i + 1) / n, 1)
                self.d.line([((x0 + (x1 - x0) * a) * S, (y0 + (y1 - y0) * a) * S), ((x0 + (x1 - x0) * b) * S, (y0 + (y1 - y0) * b) * S)],
                            fill=col, width=int(width * S))

    def rect(self, x, y, w, h, fill, radius=0):
        self.d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius * S, fill=fill)

    def arrow(self, pts, col=WHITE, width=2.0, head=10.0, both=False):
        P = [(px * S, py * S) for px, py in pts]
        self.d.line(P, fill=col, width=int(width * S), joint="curve")
        ends = [(P[-2], P[-1])] + ([(P[1], P[0])] if both else [])
        for (ax, ay), (bx, by) in ends:
            ang = math.atan2(by - ay, bx - ax)
            h = head * S
            self.d.polygon([(bx, by), (bx - h * math.cos(ang - 0.42), by - h * math.sin(ang - 0.42)),
                            (bx - h * math.cos(ang + 0.42), by - h * math.sin(ang + 0.42))], fill=col)

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.img.resize((W, H), Image.LANCZOS).save(path, optimize=True)
        print("wrote", path.relative_to(ROOT), f"{path.stat().st_size / 1e6:.2f} MB")


def header(c: Canvas, title: str, sub: str):
    c.text((70, 52), "LoopCheck", "semibold", 22, WHITE)
    c.label((70 + c.width("LoopCheck", "semibold", 22) + 20, 57), "OpenCV AI Competition 2026", FAINT)
    c.text((70, 96), title, "display", 46, WHITE)
    c.text((72, 158), sub, "regular", 19, MUTED)


# ============================================================================ 05 architecture
def architecture() -> None:
    c = Canvas()
    header(c, "Architecture", "Verified against template.yaml, loopcheck/aws_lambda.py, service.py, agent.py and app/streamlit_app.py")

    # --- top row: frontend -> API Gateway -> Lambda
    top, th = 222, 150
    nodes = [
        (70, 440, "Streamlit Community Cloud", ["app/streamlit_app.py", "loopcheck.client: server-side urllib", "endpoint from st.secrets (never shown)"], TEXT),
        (640, 450, "Amazon API Gateway · HTTP API", ["GET /health  ·  POST /inspect", "throttled: 2 req/s, burst 5", "CORS: one allowed origin"], TEXT),
        (1190, 540, "AWS Lambda · Python 3.13 · x86_64", ["handler: loopcheck.aws_lambda.handler", "OpenCV 5.0.0 (zip package) · 29 s timeout", "request + image size limits enforced"], TEXT),
    ]
    for x, w, title, lines, col in nodes:
        c.box(x, top, w, th, outline=(150, 156, 166))
        c.text((x + 22, top + 20), title, "semibold", 22, WHITE)
        for i, ln in enumerate(lines):
            c.text((x + 22, top + 62 + i * 26), ln, "regular", 17, MUTED)
    c.arrow([(70 + 440 + 8, top + 52), (640 - 8, top + 52)], WHITE, 2)
    c.label((575, top + 26), "HTTPS", MUTED, 12, "ma")
    c.label((575, top + 66), "JSON", MUTED, 12, "ma")
    c.arrow([(640 + 450 + 8, top + 52), (1190 - 8, top + 52)], WHITE, 2)
    c.label((640 + 450 + 18, top + 22), "invoke", MUTED, 12)

    # CloudWatch (dashed, observability only)
    cw_x, cw_y = 1190, 120
    c.box(cw_x + 140, cw_y - 6, 400, 74, outline=FAINT, fill=BG, dash=True, radius=10)
    c.text((cw_x + 162, cw_y + 6), "Amazon CloudWatch Logs", "semibold", 18, TEXT)
    c.text((cw_x + 162, cw_y + 34), "one metadata line per request · 7-day retention", "regular", 15, MUTED)
    c.d.line([((cw_x + 340) * S, (cw_y + 68) * S), ((cw_x + 340) * S, top * S)], fill=FAINT, width=2 * S)

    # --- Lambda internals
    ix, iy, iw, ih = 70, 430, 1660, 560
    c.box(ix, iy, iw, ih, outline=LINE, fill=(14, 15, 18), radius=16)
    c.label((ix + 24, iy + 20), "Inside the Lambda · loopcheck.service.analyze()", MUTED, 14)
    c.d.line([((1190 + 270) * S, (top + th) * S), ((1190 + 270) * S, iy * S)], fill=(150, 156, 166), width=2 * S)

    # decode/validate
    c.box(ix + 24, iy + 64, 300, 150, outline=LINE)
    c.text((ix + 46, iy + 84), "Decode + validate", "semibold", 20, WHITE)
    c.wrap((ix + 46, iy + 120), "PNG/JPEG, ≤ 2 MB and ≤ 4096 px per image; bad input → HUMAN_REVIEW", 260, "regular", 16, MUTED, 1.38)
    c.arrow([(ix + 24 + 300 + 6, iy + 139), (ix + 384 - 6, iy + 139)], WHITE, 2)

    # controller
    cx, cy, cw, ch = ix + 384, iy + 64, 520, 250
    c.box(cx, cy, cw, ch, outline=WHITE, width=2.4)
    c.label((cx + 22, cy + 20), "Agent controller · decides", WHITE, 13)
    c.text((cx + 22, cy + 46), "InspectionAgent", "semibold", 24, WHITE)
    for i, ln in enumerate(["explicit, ordered policy rules (R, A–F, C, D, E, T)",
                            "observe → choose ONE tool → record → repeat",
                            "every step logs rule, observations, params, effect",
                            "≤ 16 steps · no duplicate actions",
                            "any tool error, budget or policy fault → HUMAN_REVIEW"]):
        c.text((cx + 22, cy + 92 + i * 30), "·  " + ln, "regular", 17, TEXT)

    # tools
    tx, ty, tw = cx + cw + 70, iy + 64, 640
    c.label((tx, ty - 2), "Perception tools · OpenCV 5 · measure", BLUE, 13)
    tools = [
        ("register", "ArUco or SIFT + USAC-MAGSAC homography (+2 fallbacks)"),
        ("marker masking", "ArUco polygons excluded; marker_band_check screens them"),
        ("compare", "intensity · SSIM · gradient · colour → anomaly map"),
        ("reinspect", "independent crop views must reproduce a weak candidate"),
        ("content_check", "same item, not just the same markers"),
    ]
    for i, (name, desc) in enumerate(tools):
        y = ty + 26 + i * 52
        c.box(tx, y, tw, 46, outline=BLUE, fill=BLUE_FILL, radius=9)
        c.text((tx + 16, y + 23), name, "mono_medium", 17, (170, 210, 240), "lm")
        c.text((tx + 186, y + 23), desc, "regular", 15, TEXT, "lm")
    # call / observation arrows between controller and tools
    c.arrow([(cx + cw + 8, cy + 104), (tx - 8, cy + 104)], BLUE, 2.2)
    c.arrow([(tx - 8, cy + 160), (cx + cw + 8, cy + 160)], (150, 156, 166), 2.2)
    c.label((cx + cw + 35, cy + 82), "call", BLUE, 11, "ma")
    c.label((cx + cw + 35, cy + 170), "result", MUTED, 11, "ma")

    # response
    ry = iy + 360
    c.box(ix + 384, ry, 1250, 170, outline=RED, width=2.0)
    c.label((ix + 406, ry + 20), "Structured response · JSON", RED, 13)
    c.arrow([(cx + cw / 2, cy + ch + 6), (cx + cw / 2, ry - 6)], WHITE, 2)
    x = ix + 406
    for dname, col in DECISION.items():
        tw_ = c.width(dname, "semibold", 18) + 30
        c.rect(x, ry + 50, tw_, 34, col, 8)
        c.text((x + 15, ry + 67), dname, "semibold", 18, WHITE, "lm")
        x += tw_ + 12
    c.text((x + 10, ry + 67), "+ termination rule · reasons · step-by-step trace", "regular", 18, TEXT, "lm")
    c.text((ix + 406, ry + 108), "candidates with scores (uncalibrated heuristics) · coverage incl. NOT-inspected marker areas", "regular", 17, MUTED)
    c.text((ix + 406, ry + 136), "evidence images: aligned · anomaly heatmap · annotated overlay (JPEG, ≤ 1024 px)", "regular", 17, MUTED)

    # return path to the frontend: below the decode box, out of the container, up the left margin
    c.arrow([(ix + 384, ry + 85), (36, ry + 85), (36, top + 75), (70 - 6, top + 75)], RED, 2.2)
    c.wrap((ix + 24, ry + 104), "response returned to the app; images shown with st.image", 300, "regular", 15, MUTED, 1.35)

    # footer
    c.text((70, 1030), "No ML model, database or message queue: decisions are explicit rules over OpenCV measurements.", "regular", 19, TEXT)
    c.text((70, 1062), "The app's “Local (in-process)” mode runs the same service inside Streamlit; HTTP API mode uses the path shown here.", "regular", 17, MUTED)
    c.label((70, 1120), "Research prototype · validated on synthetic data only", FAINT, 13)
    c.save(OUT / "05-loopcheck-architecture.png")


# ============================================================================ 06 evaluation
def evaluation() -> None:
    m = json.loads((ROOT / "results" / "d3" / "test" / "metrics.json").read_text())
    o, a, meta = m["one_shot"], m["agent"], m["meta"]
    assert o["n"] == a["n"]
    rows = [  # label, hint, one-shot, agent, higher_is_better
        ("Image-level F1 (DEFECT = positive)", "higher is better", o["image_level"]["f1"], a["image_level"]["f1"], True),
        ("False-positive rate on good images", "lower is better", o["false_positive_rate_good"], a["false_positive_rate_good"], False),
        ("Accuracy of automatic decisions", "higher is better", o["automatic_accuracy"], a["automatic_accuracy"], True),
        ("Automatic decision coverage", "share decided without a person", o["automatic_coverage"], a["automatic_coverage"], True),
        ("HUMAN_REVIEW rate", "deliberate escalations", o["human_review_rate"], a["human_review_rate"], None),
        ("Task success", "decision matches the expected outcome", o["task_success_rate"], a["task_success_rate"], True),
    ]
    c = Canvas()
    header(c, "Adaptive agent vs one-shot pipeline",
           f"Held-out test split: {a['n']} synthetic cases (seed {meta['seed']}), thresholds frozen on the development split before testing")
    # qualification banner
    c.box(70, 196, 1660, 62, outline=RED, fill=(30, 15, 17), width=2.0, radius=10)
    c.rect(70, 196, 8, 62, RED, 3)
    c.text((100, 227), "Synthetic held-out evaluation — not real-world validation", "semibold", 26, WHITE, "lm")
    c.text((1706, 227), "scores are heuristics, not probabilities", "regular", 18, (240, 170, 170), "rm")

    # legend
    lx, ly = 70, 300
    c.rect(lx, ly, 18, 18, GREY_BAR, 3)
    c.text((lx + 28, ly + 9), "One-shot pipeline (register + compare, mapped mechanically)", "regular", 17, TEXT, "lm")
    c.rect(lx + 560, ly, 18, 18, BLUE, 3)
    c.text((lx + 588, ly + 9), "Adaptive agent (D3 controller)", "regular", 17, TEXT, "lm")

    # bars
    bx, bw, y0, rh = 470, 560, 350, 108
    for i, (lab, hint, ov, av, hib) in enumerate(rows):
        y = y0 + i * rh
        c.text((70, y + 22), lab, "semibold", 19, WHITE)
        c.text((70, y + 50), hint, "regular", 15, MUTED)
        for j, (v, col) in enumerate(((ov, GREY_BAR), (av, BLUE))):
            yy = y + 12 + j * 38
            c.rect(bx, yy, bw, 28, (24, 26, 30), 5)
            if v > 0:
                c.rect(bx, yy, max(bw * v, 6), 28, col, 5)
            c.text((bx + bw + 14, yy + 14), f"{v:.3f}", "mono_medium", 19, WHITE if j else TEXT, "lm")
        if hib is not None:
            better = (av > ov) if hib else (av < ov)
            mark = "agent better" if better else ("same" if av == ov else "agent lower")
            c.label((bx + bw + 92, y + 34), mark, BLUE if better else MUTED, 12)

    # trade-off panel
    px, py, pw, ph = 1250, 300, 480, 690
    c.box(px, py, pw, ph, outline=LINE)
    c.label((px + 24, py + 22), "The trade-off, honestly", MUTED, 14)
    yy = py + 60
    facts = [
        ("Fewer automatic decisions", f"coverage {o['automatic_coverage']:.3f} → {a['automatic_coverage']:.3f}; "
                                      f"HUMAN_REVIEW {o['human_review_rate']:.3f} → {a['human_review_rate']:.3f}"),
        ("Fewer automatic errors", f"errors among automatic decisions {o['automatic_errors']} → {a['automatic_errors']}; "
                                   f"defects approved {o['critical_errors_approved']} → {a['critical_errors_approved']}"),
        ("Lower DEFECT recall", f"{o['image_level']['recall']:.3f} → {a['image_level']['recall']:.3f}: the missing defects were escalated, not approved"),
        ("More good trays escalated", f"good images not auto-approved {o['good_not_auto_approved_rate']:.3f} → {a['good_not_auto_approved_rate']:.3f}"),
        ("Slower", f"mean latency {o['seconds']['mean']:.2f} s → {a['seconds']['mean']:.2f} s (4-core CPU, 800×600)"),
    ]
    for head, body in facts:
        c.text((px + 24, yy), head, "semibold", 19, WHITE)
        yy = c.wrap((px + 24, yy + 30), body, pw - 48, "regular", 16, TEXT, 1.4) + 14
    yy += 4
    c.d.line([((px + 24) * S, yy * S), ((px + pw - 24) * S, yy * S)], fill=LINE, width=2)
    yy = c.wrap((px + 24, yy + 16), "“0 errors” holds for this 180-case synthetic split only; on the development split the agent made "
                                    "1 automatic error (1 approved defect). Real-photograph performance is not established.",
                pw - 48, "regular", 15, MUTED, 1.42)

    c.label((70, 1030), "Source: results/d3/test/metrics.json (committed) · details: docs/d3_agent.md", FAINT, 13)
    c.text((70, 1062), "One-shot decisions are mapped mechanically: no regions → APPROVE, regions → DEFECT, registration failure → HUMAN_REVIEW.",
           "regular", 16, MUTED)
    c.label((70, 1120), "Research prototype · validated on synthetic data only", FAINT, 13)
    c.save(OUT / "06-evaluation-results.png")


if __name__ == "__main__":
    architecture()
    evaluation()
