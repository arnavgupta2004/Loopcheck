"""The seven scenes of the LoopCheck film. `frame(t)` returns the RGBA frame at time t.

All imagery is real: app screenshots captured from the running Streamlit app
(video/assets/app, see capture/capture_app.py) and pipeline outputs produced by the real
service (video/assets/pipeline, see make_assets.py). Geometry (homography, boxes, control
positions) is read from those assets, never typed in by hand.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter

from gfx import (ASSETS, BLUE, BLUE_DIM, DECISION, FAINT, H, LINE, MUTED, PANEL, RED, TEXT, WHITE, W, arrow, black_frame,
                 brackets, chip, circle, clamp, click_ring, crop_to, cursor, decision_badge, draw_paragraph, draw_text,
                 ease_in_out, ease_out, fade, fit, glow_dot, label, lerp, lines, load, new_frame, panel_image, paste, prog,
                 rect, text_width, window, with_alpha)
from timeline import DURATION, SCENES

SCENE_BY_ID = {s["id"]: s for s in SCENES}


# ============================================================================ data
@lru_cache(maxsize=None)
def case(key: str) -> dict:
    return json.loads((ASSETS / "pipeline" / key / "case.json").read_text())


@lru_cache(maxsize=None)
def capture() -> dict:
    return json.loads((ASSETS / "app" / "capture.json").read_text())


@lru_cache(maxsize=None)
def smoke() -> tuple[str, list[str], str]:
    """Real smoke-test output. A sanitised deployed run (assets/api/smoke_deployed.txt) takes precedence."""
    dep = ASSETS / "api" / "smoke_deployed.txt"
    if dep.exists():
        return "deployed", dep.read_text().splitlines(), "SMOKE TEST · DEPLOYED API GATEWAY + LAMBDA (URL REDACTED)"
    return ("local", (ASSETS / "api" / "smoke_local.txt").read_text().splitlines(),
            "SMOKE TEST · SAME LAMBDA HANDLER SERVED LOCALLY (loopcheck.local_server)")


def img_np(key: str, name: str) -> np.ndarray:
    return cv2.imread(str(ASSETS / "pipeline" / key / f"{name}.png"), cv2.IMREAD_COLOR)


def pil(key: str, name: str) -> Image.Image:
    return load(f"pipeline/{key}/{name}.png")


def H_ref_to_insp(key: str) -> np.ndarray:
    return np.array(case(key)["H_ref_to_insp"], np.float64)


def project(H_: np.ndarray, pts) -> np.ndarray:
    p = np.c_[np.asarray(pts, np.float64), np.ones(len(pts))] @ H_.T
    return p[:, :2] / p[:, 2:3]


# ============================================================================ chrome
def chrome(c: Image.Image, t: float, sid: str, alpha: float) -> None:
    """Section header shared by S2-S6: brand, section index and title, prototype tag."""
    if alpha <= 0:
        return
    idx = [s["id"] for s in SCENES].index(sid) + 1
    bw = text_width("LoopCheck", "semibold", 24)
    draw_text(c, "LoopCheck", 120, 50, "semibold", 24, WHITE, alpha)
    lines(c, [((120 + bw + 18, 57), (120 + bw + 18, 77))], LINE, 1.5, alpha)
    label(c, f"{idx:02d}  {SCENE_BY_ID[sid]['label']}", 120 + bw + 36, 55, MUTED, alpha)
    label(c, "Research prototype · synthetic data", W - 120, 55, FAINT, alpha, anchor="ra")


def scene_alpha(t: float, sid: str, fin=0.45, fout=0.45) -> float:
    s = SCENE_BY_ID[sid]
    return window(t, s["start"], s["end"], fin, fout)


# ============================================================================ S1 / S7 macro tray
MACRO_SCALE = 3


@lru_cache(maxsize=1)
def macro_sources() -> tuple[Image.Image, Image.Image]:
    """Graded, upscaled real inspection image of case 02 (sharp + defocused copies)."""
    src = img_np("missing", "inspection")
    big = cv2.resize(src, None, fx=MACRO_SCALE, fy=MACRO_SCALE, interpolation=cv2.INTER_LANCZOS4)
    big = cv2.addWeighted(big, 1.35, cv2.GaussianBlur(big, (0, 0), 2.2), -0.35, 0)  # gentle unsharp mask
    f = cv2.cvtColor(big, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    lum = f @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    f = lum[..., None] + (f - lum[..., None]) * 0.72                      # desaturate
    f = np.clip((f - 0.5) * 1.12 + 0.47, 0, 1) ** 1.12                    # contrast, deeper shadows
    shadow_tint = np.array([0.92, 1.0, 1.12], np.float32)
    f = f * (shadow_tint * (1 - lum[..., None]) + lum[..., None])        # cool shadows
    f *= 0.66
    sharp = Image.fromarray((np.clip(f, 0, 1) * 255).astype(np.uint8), "RGB")
    blur = sharp.filter(ImageFilter.GaussianBlur(9))
    return sharp, blur


def missing_box_insp() -> tuple[float, float, float, float]:
    """Ground-truth missing-part box (reference px) mapped into the inspection image by the ESTIMATED homography."""
    x, y, w, h = case("missing")["ground_truth_boxes_xywh_reference"][0]
    q = project(H_ref_to_insp("missing"), [(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
    return q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()


def macro_camera(t: float, t0: float, t1: float, w0: float, w1: float, d0, d1):
    x0, y0, x1, y1 = missing_box_insp()
    fx, fy = (x0 + x1) / 2 * MACRO_SCALE, (y0 + y1) / 2 * MACRO_SCALE
    p = ease_in_out(prog(t, t0, t1)) * 0.85 + prog(t, t0, t1) * 0.15
    cw = lerp(w0, w1, p)
    cx, cy = fx + lerp(d0[0], d1[0], p), fy + lerp(d0[1], d1[1], p)
    ch = cw * H / W
    return cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2


@lru_cache(maxsize=8)
def _dof_mask(focus_y: int) -> Image.Image:
    yy = np.arange(H, dtype=np.float32)
    m = np.clip(1 - (np.abs(yy - focus_y) - 170) / 330, 0, 1) ** 1.6
    return Image.fromarray((np.repeat(m[:, None], W, 1) * 255).astype(np.uint8), "L")


@lru_cache(maxsize=1)
def _title_scrim() -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    a = np.clip((yy - H * 0.38) / (H * 0.62), 0, 1) ** 1.3 * np.clip(1.25 - xx / (W * 0.95), 0, 1)
    img = Image.new("RGBA", (W, H), (6, 7, 9, 255))
    img.putalpha(Image.fromarray((np.clip(a, 0, 1) * 235).astype(np.uint8), "L"))
    return img


@lru_cache(maxsize=1)
def _top_scrim() -> Image.Image:
    yy = np.arange(H, dtype=np.float32)
    a = np.clip(1 - yy / 190, 0, 1) ** 1.5
    img = Image.new("RGBA", (W, H), (6, 7, 9, 255))
    img.putalpha(Image.fromarray((np.repeat(a[:, None], W, 1) * 200).astype(np.uint8), "L"))
    return img


def macro_frame(box) -> Image.Image:
    sharp, blur = macro_sources()
    a = crop_to(sharp, box, (W, H))
    b = crop_to(blur, box, (W, H))
    x0, y0, x1, y1 = missing_box_insp()
    fy = ((y0 + y1) / 2 * MACRO_SCALE - box[1]) / (box[3] - box[1]) * H
    out = Image.composite(a, b, _dof_mask(int(round(fy / 8) * 8)))
    return out.convert("RGBA")


def to_frame(box, px, py):
    return ((px * MACRO_SCALE - box[0]) / (box[2] - box[0]) * W, (py * MACRO_SCALE - box[1]) / (box[3] - box[1]) * H)


def s1(t: float) -> Image.Image:
    box = macro_camera(t, 0.0, 8.0, 1560, 1150, (-170, 70), (40, 20))
    c = macro_frame(box)
    c.alpha_composite(_title_scrim())
    # scan sweep
    if 2.2 <= t <= 3.8:
        p = ease_in_out(prog(t, 2.2, 3.7))
        x = lerp(-60, W + 60, p)
        rect(c, x - 140, 0, 140, H, BLUE, alpha=0.05 * (1 - abs(p - 0.5) * 1.2), fill=True)
        lines(c, [((x, 0), (x, H))], BLUE, 2.0, 0.85)
    # defect indicator on the empty slot
    x0, y0, x1, y1 = missing_box_insp()
    (fx0, fy0), (fx1, fy1) = to_frame(box, x0, y0), to_frame(box, x1, y1)
    pad = 16
    if t >= 4.0:
        g = ease_out(prog(t, 4.0, 4.5))
        brackets(c, fx0 - pad, fy0 - pad, fx1 - fx0 + 2 * pad, fy1 - fy0 + 2 * pad, RED, arm=26, width=3, alpha=g, grow=g)
        click_ring(c, (fx0 + fx1) / 2, (fy0 + fy1) / 2, prog(t, 4.0, 5.2), RED)
        la = ease_out(prog(t, 4.35, 4.9))
        rect(c, fx1 + pad + 10, fy0 - pad - 8, text_width("EXPECTED COMPONENT", "mono_medium", 17, 2.2) + 26, 70, (8, 9, 11),
             alpha=la * 0.78, fill=True, radius=6)
        label(c, "Expected component", fx1 + pad + 22, fy0 - pad + 2, RED, la)
        label(c, "Not present", fx1 + pad + 22, fy0 - pad + 30, WHITE, la * 0.85)
    c.alpha_composite(_top_scrim())
    label(c, "Synthetic tray · LoopCheck demo case 02", 120, 60, TEXT, ease_out(prog(t, 1.2, 2.0)) * 0.85)
    # titles
    for text, ty, t0 in (("Small defects.", 690, 1.0), ("High-stakes decisions.", 800, 2.2)):
        p = ease_out(prog(t, t0, t0 + 0.8))
        draw_text(c, text, 116, ty, "display", 96, WHITE, p, dy=(1 - p) * 26)
    p = ease_out(prog(t, 4.9, 5.6))
    draw_text(c, "Inspection compares what should be present with what actually is.", 122, 930, "regular", 32, TEXT, p, dy=(1 - p) * 12)
    c = fade(c, 1 - ease_out(prog(t, 0.0, 1.1)))
    return fade(c, ease_in_out(prog(t, 7.25, 8.0)) * 0.92)


# ============================================================================ S2 reveal
@lru_cache(maxsize=1)
def macro_backdrop() -> Image.Image:
    box = macro_camera(8.0, 0.0, 8.0, 1560, 1150, (-170, 70), (40, 20))
    img = macro_frame(box).filter(ImageFilter.GaussianBlur(14))
    return with_alpha(img, 0.16)


@lru_cache(maxsize=16)
def sized(key: str, name: str, w: int, h: int) -> Image.Image:
    return fit(pil(key, name), w, h)


def comparison(divider: float, w: int, h: int, show_box: float) -> Image.Image:
    ref, ali = sized("missing", "reference", w, h), sized("missing", "aligned", w, h)
    out = ref.copy()
    cut = int(round(w * clamp(divider)))
    if cut < w:
        out.paste(ali.crop((cut, 0, w, h)), (cut, 0))
    if show_box > 0:
        x, y, bw, bh = case("missing")["ground_truth_boxes_xywh_reference"][0]
        s = w / 800
        brackets(out, x * s - 8, y * s - 8, bw * s + 16, bh * s + 16, RED, arm=14, width=2.5, alpha=show_box, grow=show_box)
    return out


def s2(t: float) -> Image.Image:
    c = new_frame()
    paste(c, macro_backdrop(), 0, 0, 1.0)
    sa = scene_alpha(t, "S2", 0.4, 0.45)
    # wordmark
    wa = ease_out(prog(t, 8.35, 9.3)) * (1 - ease_in_out(prog(t, 10.6, 11.2)))
    if wa > 0:
        wy = 430 - ease_in_out(prog(t, 10.6, 11.2)) * 30
        draw_text(c, "LoopCheck", W / 2, wy, "display", 164, WHITE, wa, anchor="ma", dy=(1 - ease_out(prog(t, 8.35, 9.3))) * 30)
        rw = 140 * ease_out(prog(t, 8.9, 9.7))
        rect(c, W / 2 - rw / 2, wy + 222, rw, 4, RED, alpha=wa, fill=True)
        pa = ease_out(prog(t, 9.25, 10.0)) * (1 - ease_in_out(prog(t, 10.6, 11.2)))
        draw_text(c, "Agentic visual inspection", W / 2, wy + 258, "display_light", 48, TEXT, pa, anchor="ma")
    chrome(c, t, "S2", ease_out(prog(t, 10.9, 11.5)) * sa)
    # reference vs inspection
    pw, ph = 720, 540
    enter = ease_out(prog(t, 11.0, 11.8))
    merge = ease_in_out(prog(t, 13.2, 14.0))
    to_side = ease_in_out(prog(t, 16.3, 17.0))
    if t >= 11.0 and merge < 1:
        for i, (key, name, lab) in enumerate((("missing", "reference", "Reference"), ("missing", "inspection", "Inspection"))):
            x0 = (200, 1000)[i]
            x = lerp(x0, 600, merge)
            a = enter * (1 - ease_in_out(prog(t, 13.5, 14.0)))
            img = sized(key, name, pw, ph)
            panel_image(c, img, x, 290 + (1 - enter) * 30, alpha=a)
            label(c, lab, x, 250 + (1 - enter) * 30, MUTED, a)
    if t >= 13.6:
        a = ease_out(prog(t, 13.6, 14.1))
        div = 1.0 - ease_in_out(prog(t, 14.0, 15.4)) * 0.62
        div = lerp(div, 0.30, ease_in_out(prog(t, 15.4, 16.2)))
        s = lerp(1.0, 0.86, to_side)
        w2, h2 = int(pw * s), int(ph * s)
        x = lerp(600, 160, to_side)
        y = lerp(290, 300, to_side)
        img = comparison(div, w2, h2, ease_out(prog(t, 15.0, 15.6)))
        panel_image(c, img, x, y, alpha=a)
        lx = x + w2 * div
        lines(c, [((lx, y), (lx, y + h2))], WHITE, 2.0, a)
        circle(c, lx, y + h2 / 2, 11, WHITE, alpha=a, fill=True)
        circle(c, lx, y + h2 / 2, 11, (30, 30, 30), 1.5, a)
        label(c, "Reference", x, y - 40, MUTED, a)
        label(c, "Inspection · registered", x + w2, y - 40, BLUE, a, anchor="ra")
    # statement
    if t >= 17.0:
        x = 1000
        p = ease_out(prog(t, 17.0, 17.7))
        draw_text(c, "Not a single opaque label.", x, 300, "display", 58, WHITE, p, dy=(1 - p) * 18)
        draw_paragraph(c, "A transparent controller chooses each tool, records why it ran, and returns the evidence "
                          "with the decision.", x, 392, 720, "regular", 31, TEXT, 1.0, stagger=0.18, t_local=t - 17.4)
        cx = x
        for i, word in enumerate(("Decision", "Rule that ended the run", "Tool trace", "Evidence images")):
            a = ease_out(prog(t, 18.0 + i * 0.18, 18.5 + i * 0.18))
            col = WHITE if i == 0 else (BLUE if i < 3 else RED)
            wdt = chip(c, word, cx, 560 + (i // 2) * 58, col, a, 17)
            cx = cx + wdt + 14 if i % 2 == 0 else x
    return fade(c, 1 - sa)


# ============================================================================ S3 workflow
STAGE = (120, 400, 800, 600)  # x, y, w, h (= 800x600, the working resolution of the demo cases)
TOOLS = [  # (key, title, subtitle, kind, x, w)
    ("in", "Inputs", "reference + inspection", "input", 120, 300),
    ("R0", "R0 · register", "ArUco or SIFT → homography", "tool", 450, 300),
    ("R1", "R1 · compare", "anomaly map from 4 cues", "tool", 780, 300),
    ("C0", "C0 · confirm", "strong first-pass evidence", "rule", 1110, 300),
    ("T1", "T1 · decide", "terminate with a decision", "decision", 1440, 360),
]
LANE_Y, TOOL_Y, TOOL_H = 140, 252, 92


def warp_stage(a: float) -> tuple[Image.Image, np.ndarray]:
    """Inspection warped by the interpolated REAL homography (a=0 raw, a=1 aligned)."""
    ins = img_np("missing", "inspection")
    Hm = H_ref_to_insp("missing")
    M = (1 - a) * np.eye(3) + a * (Hm / Hm[2, 2])
    out = cv2.warpPerspective(ins, M, (800, 600), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=(12, 13, 15))
    corners = np.array([(0, 0), (800, 0), (800, 600), (0, 600)], np.float64)
    q = project(np.linalg.inv(M), project(Hm, corners))  # reference frame outline, in the displayed image
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB)).convert("RGBA"), q


def s3(t: float) -> Image.Image:
    c = new_frame()
    sa = scene_alpha(t, "S3", 0.45, 0.45)
    chrome(c, t, "S3", 1.0)
    cm = case("missing")
    # controller lane
    lp = ease_in_out(prog(t, 20.2, 21.2))
    lw = 1680 * lp
    rect(c, 120, LANE_Y, lw, 64, WHITE, 1.8, 0.9, radius=10)
    la = ease_out(prog(t, 20.7, 21.3))
    label(c, "Controller", 146, LANE_Y + 23, WHITE, la)
    draw_text(c, "explicit ordered rules  ·  one tool per step  ·  at most 16 steps  ·  any tool error → HUMAN_REVIEW",
              320, LANE_Y + 32, "regular", 22, TEXT, la, anchor="lm")
    # legend
    lg = ease_out(prog(t, 21.0, 21.6))
    rect(c, 1290, 92, 14, 14, WHITE, 1.6, lg)
    label(c, "Policy", 1312, 90, MUTED, lg)
    rect(c, 1420, 92, 14, 14, BLUE, alpha=lg, fill=True)
    label(c, "OpenCV 5 tools", 1442, 90, MUTED, lg)
    # tool chips + dispatch lines
    active = {"R0": (22.4, 26.3), "R1": (26.4, 29.0), "C0": (29.0, 30.2), "T1": (30.2, 34.0)}
    for i, (k, title, sub, kind, x, w) in enumerate(TOOLS):
        a = ease_out(prog(t, 20.7 + i * 0.16, 21.3 + i * 0.16))
        col = {"input": MUTED, "tool": BLUE, "rule": WHITE, "decision": DECISION["DEFECT"]}[kind]
        on = k in active and t >= active[k][0]
        hot = k in active and active[k][0] <= t < active[k][1]
        if on and kind != "decision":
            rect(c, x, TOOL_Y, w, TOOL_H, col, alpha=a * (0.20 if hot else 0.08), fill=True, radius=10)
        if k == "T1" and t >= 30.2:
            rect(c, x, TOOL_Y, w, TOOL_H, col, alpha=a * ease_out(prog(t, 30.2, 30.6)), fill=True, radius=10)
        rect(c, x, TOOL_Y, w, TOOL_H, col, 2.0 if kind != "input" else 1.4, a * (1 if (on or kind == "input") else 0.55), radius=10)
        tcol = WHITE if (on or kind == "input") else MUTED
        if k == "T1" and t >= 30.2:
            draw_text(c, "DEFECT · rule T1", x + 22, TOOL_Y + 20, "semibold", 25, WHITE, a)
            draw_text(c, "1 confirmed defect region", x + 22, TOOL_Y + 56, "regular", 19, WHITE, a * 0.9)
        else:
            draw_text(c, title, x + 22, TOOL_Y + 20, "semibold", 25, tcol, a)
            draw_text(c, sub, x + 22, TOOL_Y + 56, "regular", 19, MUTED, a)
        if i > 0:
            arrow(c, TOOLS[i - 1][4] + TOOLS[i - 1][5] + 4, TOOL_Y + TOOL_H / 2, x - 6, TOOL_Y + TOOL_H / 2, FAINT, 1.6, a, 8)
        if k in active:  # dispatch from the controller lane
            dx = x + w / 2
            lines(c, [((dx, LANE_Y + 64), (dx, TOOL_Y))], col if on else FAINT, 1.6, a * (1 if on else 0.5))
            if hot:
                q = prog(t, active[k][0], active[k][0] + 0.45)
                if q < 1:
                    glow_dot(c, dx, lerp(LANE_Y + 64, TOOL_Y, ease_in_out(q)), col, 4.5, 1.0)
    # stage
    sx, sy, sw, sh = STAGE
    st_a = ease_out(prog(t, 21.6, 22.3))
    reg = ease_in_out(prog(t, 22.8, 25.5))
    img, q = warp_stage(reg)
    if t >= 26.6:  # R1: heatmap wipe over the aligned image
        hm = pil("missing", "heatmap")
        wp = ease_in_out(prog(t, 26.6, 27.9))
        cut = int(round(800 * wp))
        if cut > 0:
            blend = Image.blend(img.convert("RGB"), hm.convert("RGB"), 0.82).convert("RGBA")
            img = img.copy()
            img.paste(blend.crop((0, 0, cut, 600)), (0, 0))
    panel_image(c, img, sx, sy, radius=10, alpha=st_a)
    stage_label = "Inspection · raw" if t < 22.8 else ("Registering · real homography" if t < 25.6 else
                  ("Aligned to the reference frame" if t < 26.6 else "Anomaly map (uncalibrated heuristic)"))
    label(c, stage_label, sx, sy - 34, BLUE if 22.8 <= t < 26.6 else MUTED, st_a)
    if 22.8 <= t < 26.6:
        qa = ease_out(prog(t, 22.8, 23.2)) * (1 - prog(t, 26.0, 26.6))
        pts = [(sx + px, sy + py) for px, py in q]
        lines(c, list(zip(pts, pts[1:] + pts[:1])), BLUE, 2.2, qa)
        for px, py in pts:
            circle(c, px, py, 5, BLUE, alpha=qa, fill=True)
        if t >= 25.6:
            g = prog(t, 25.6, 26.1)
            rect(c, sx + 2, sy + 2, sw - 4, sh - 4, WHITE, 3.0, (1 - g) * 0.9)
    if t >= 28.0:
        cand = cm["candidates"][0]
        x, y, bw, bh = cand["bbox_xywh_reference"]
        g = ease_out(prog(t, 28.0, 28.45))
        brackets(c, sx + x - 10, sy + y - 10, bw + 20, bh + 20, RED, 16, 3, g, g)
    # step log (values from the real trace / case.json)
    log = [
        (22.6, "step 0", "R0", "register(variant=default)", WHITE),
        (25.6, "", "", f"→ ok · {cm['registration_method']} homography", BLUE),
        (26.6, "step 1", "R1", "compare(variant=default)", WHITE),
        (28.1, "", "", f"→ 1 candidate · peak {cm['candidates'][0]['peak']:.2f} · {cm['candidates'][0]['area_px']} px", BLUE),
        (29.1, "note", "C0", "peak ≥ 0.75, area ≥ 400 px, no lighting shift", WHITE),
        (29.5, "", "", "→ candidate confirmed without re-inspection", BLUE),
        (30.3, "end", "T1", f"{cm['decision']} · {cm['reasons'][0]}", (240, 120, 120)),
    ]
    lx, ly = 1000, 410
    label(c, "Agent trace", lx, ly - 44, MUTED, st_a)
    for i, (t0, step, rule, txt, col) in enumerate(log):
        a = ease_out(prog(t, t0, t0 + 0.35))
        if a <= 0:
            continue
        yy = ly + i * 46
        if step:
            draw_text(c, step, lx, yy, "mono", 21, FAINT, a)
            draw_text(c, rule, lx + 92, yy, "mono_medium", 21, BLUE if rule.startswith("R") else WHITE, a)
        draw_text(c, txt, lx + 160, yy, "mono", 21, col, a, dy=(1 - a) * 6)
    # conditional tools
    ca = ease_out(prog(t, 31.0, 31.6))
    label(c, "Further tools run only when observations call for them", lx, 790, MUTED, ca)
    extra = ["A1 register (alternative)", "F1 content_check", "B1 robust compare", "D1 sensitive scan", "C1 reinspect", "E1 marker band"]
    xx, yy = lx, 830
    for i, e in enumerate(extra):
        a = ease_out(prog(t, 31.2 + i * 0.16, 31.6 + i * 0.16))
        wdt = text_width(e.upper(), "mono_medium", 16, 1.6) + 28
        if xx + wdt > 1800:
            xx, yy = lx, yy + 52
        chip(c, e, xx, yy, BLUE_DIM if i != 4 else BLUE, a, 16)
        xx += wdt + 12
    return fade(c, 1 - sa)


# ============================================================================ S4 real app
VIEW = (600, 168, 1200, 780)  # on-screen viewport for the screenshots
ASPECT = VIEW[2] / VIEW[3]
CAM = {  # (cx, cy, width) in screenshot px (2x DPR)
    "overview": (1600, 1060, 3200),
    "sidebar": (620, 1120, 1300),
    "banner": (1900, 960, 2520),
    "images": (1900, 2470, 2440),
    "overlay": (1130, 2511, 1100),
    "trace": (1900, 1290, 2480),
}
SHOTS = [  # (t_start, t_end, screenshot)
    (34.0, 35.55, "landing_eval"), (35.35, 40.8, "clean_images"), (40.6, 46.8, "missing_images"),
    (46.6, 49.7, "ambiguous_images"), (49.4, 53.0, "ambiguous_trace"),
]
CAM_KEYS = [  # (time, camera)
    (34.0, "overview"), (34.6, "overview"), (35.15, "sidebar"), (35.6, "sidebar"), (36.5, "banner"), (38.3, "banner"),
    (39.25, "images"), (39.3, "images"), (39.95, "sidebar"), (40.9, "sidebar"), (41.8, "banner"), (43.2, "banner"),
    (44.3, "overlay"), (45.25, "overlay"), (45.95, "sidebar"), (46.9, "sidebar"), (47.8, "banner"), (49.2, "banner"),
    (50.2, "trace"), (53.0, "trace"),
]


def camera(t: float):
    ks = CAM_KEYS
    if t <= ks[0][0]:
        k0 = k1 = ks[0]
        p = 0
    else:
        for k0, k1 in zip(ks, ks[1:]):
            if t <= k1[0]:
                break
        p = ease_in_out(prog(t, k0[0], k1[0]))
    a, b = CAM[k0[1]], CAM[k1[1]]
    w = math.exp(lerp(math.log(a[2]), math.log(b[2]), p))
    cx, cy = lerp(a[0], b[0], p), lerp(a[1], b[1], p)
    h = w / ASPECT
    cx = clamp(cx, w / 2, 3200 - w / 2)
    cy = clamp(cy, h / 2, 4000 - h / 2)
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def src_to_screen(box, sx, sy):
    return VIEW[0] + (sx - box[0]) / (box[2] - box[0]) * VIEW[2], VIEW[1] + (sy - box[1]) / (box[3] - box[1]) * VIEW[3]


def center(b):
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


CLICKS = [35.2, 40.05, 40.45, 46.05, 46.45]


def pointer(t: float):
    """Pointer path in screenshot px, aimed at the REAL control positions recorded at capture."""
    boxes = capture()["clean_images"]["boxes"]
    run, sel = center(boxes["run_button"]), center(boxes["case_select"])
    sel = (sel[0] + 120, sel[1] + 8)
    rest, park = (1500, 760), (2980, 880)  # park: empty area right of the reasons list
    path = [(34.3, rest), (35.1, run), (35.9, run), (36.6, park),
            (39.4, park), (39.98, sel), (40.15, sel), (40.4, run), (41.2, run), (41.9, park),
            (45.3, park), (45.98, sel), (46.15, sel), (46.4, run), (47.2, run), (47.9, park), (53.0, park)]
    if t <= path[0][0]:
        return path[0][1]
    for (t0, p0), (t1, p1) in zip(path, path[1:]):
        if t <= t1:
            e = ease_in_out(prog(t, t0, t1))
            return lerp(p0[0], p1[0], e), lerp(p0[1], p1[1], e)
    return path[-1][1]


CASE_CAPTIONS = [  # (t_in, t_out, case key, index)
    (35.5, 40.7, "clean", "01"), (40.8, 46.7, "missing", "02"), (46.8, 53.0, "ambiguous", "03"),
]


def s4(t: float) -> Image.Image:
    c = new_frame()
    sa = scene_alpha(t, "S4", 0.45, 0.45)
    chrome(c, t, "S4", 1.0)
    vx, vy, vw, vh = VIEW
    enter = ease_out(prog(t, 34.0, 34.6))
    box = camera(t)
    # cross-dissolve between consecutive real screenshots
    layers = []
    for t0, t1, name in SHOTS:
        a = min(ease_in_out(prog(t, t0, t0 + 0.4)) if t0 > 34.0 else 1.0, 1 - ease_in_out(prog(t, t1 - 0.4, t1)) if t1 < 53.0 else 1.0)
        if a > 0:
            layers.append((name, a))
    view = Image.new("RGBA", (vw, vh), PANEL + (255,))
    for name, a in layers:
        shot = load(f"app/{name}.jpg")
        view.alpha_composite(with_alpha(crop_to(shot, box, (vw, vh)), a))
    y_off = (1 - enter) * 26
    panel_image(c, view, vx, vy + y_off, radius=14, alpha=enter)
    label(c, "Live app · Streamlit · local backend (in-process)", vx, vy - 40 + y_off, MUTED, enter)
    circle(c, vx + vw - 10, vy - 30 + y_off, 5, DECISION["APPROVE"], alpha=enter, fill=True)
    label(c, "running", vx + vw - 26, vy - 40 + y_off, MUTED, enter, anchor="ra")
    draw_text(c, "Real screenshots of the running app on bundled synthetic cases; pointer added for guidance.",
              vx, vy + vh + 22 + y_off, "regular", 19, FAINT, enter)
    # pointer over the real control positions
    if 34.3 <= t <= 52.6:
        px, py = pointer(t)
        fx, fy = src_to_screen(box, px, py)
        pressed = max([1 - abs(t - ct) / 0.12 for ct in CLICKS if abs(t - ct) < 0.12] + [0])
        for ct in CLICKS:
            click_ring(c, fx, fy, prog(t, ct, ct + 0.5))
        if vx < fx < vx + vw and vy < fy < vy + vh:
            cursor(c, fx, fy, ease_out(prog(t, 34.3, 34.6)) * (1 - prog(t, 52.3, 52.6)), pressed)
    # left caption column
    if t < 35.6:
        a = ease_out(prog(t, 34.2, 34.8)) * (1 - ease_in_out(prog(t, 35.3, 35.6)))
        draw_text(c, "Three bundled cases,", 120, 330, "display", 44, WHITE, a)
        draw_text(c, "three different outcomes.", 120, 386, "display", 44, WHITE, a)
        draw_paragraph(c, "Each run below is the real controller inside the app; decisions are read from the page at capture.",
                       122, 470, 420, "regular", 24, MUTED, a)
    for t_in, t_out, key, idx in CASE_CAPTIONS:
        a = ease_out(prog(t, t_in, t_in + 0.5)) * (1 - ease_in_out(prog(t, t_out - 0.35, t_out)))
        if a <= 0:
            continue
        cs = case(key)
        dy = (1 - ease_out(prog(t, t_in, t_in + 0.5))) * 16
        label(c, f"Case {idx}", 120, 300 + dy, MUTED, a)
        title = {"clean": "Clean, identical tray", "missing": "Missing part", "ambiguous": "Ambiguous tiny object"}[key]
        draw_text(c, title, 118, 336 + dy, "display", 40, WHITE, a)
        da = a * ease_out(prog(t, t_in + 0.25, t_in + 0.7))
        decision_badge(c, cs["decision"], cs["termination_rule"], 120, 404 + dy, da, 30)
        label(c, "Tool sequence", 120, 500 + dy, MUTED, a)
        for i, step in enumerate(cs["tool_sequence"]):
            r, tool = step.split(":")
            sa_i = a * ease_out(prog(t, t_in + 0.4 + i * 0.12, t_in + 0.8 + i * 0.12))
            draw_text(c, r, 120, 536 + i * 34 + dy, "mono_medium", 21, BLUE if r.startswith("R") else WHITE, sa_i)
            draw_text(c, tool, 170, 536 + i * 34 + dy, "mono", 21, TEXT, sa_i)
        reason = cs["reasons"][0]
        reason = reason.replace("candidate(s)", "candidate").replace("region(s)", "region")
        draw_paragraph(c, reason[0].upper() + reason[1:] + ".", 122, 560 + len(cs["tool_sequence"]) * 34 + dy, 420, "regular", 22, MUTED, a * 0.95)
    return fade(c, 1 - sa)


# ============================================================================ S5 evidence + architecture
def zoom_crop(key: str, name: str, cx: float, cy: float, win_w: float, out_w: int, out_h: int) -> Image.Image:
    win_h = win_w * out_h / out_w
    return crop_to(pil(key, name), (cx - win_w / 2, cy - win_h / 2, cx + win_w / 2, cy + win_h / 2), (out_w, out_h), Image.BICUBIC)


ARCH = [  # key, title, lines, x, w, colour
    ("client", "Streamlit client", ["app/streamlit_app.py", "or loopcheck.client"], 120, 330, MUTED),
    ("apigw", "Amazon API Gateway", ["HTTP API · GET /health", "POST /inspect · 2 req/s, burst 5"], 510, 400, WHITE),
    ("lambda", "AWS Lambda", ["Python 3.13 · x86_64", "OpenCV 5.0.0 (zip package)"], 970, 360, WHITE),
    ("ctrl", "LoopCheck controller", ["explicit rules + OpenCV 5 tools", "register · compare · reinspect · …"], 1390, 410, BLUE),
]
ARCH_Y, ARCH_H = 262, 118


def s5(t: float) -> Image.Image:
    c = new_frame()
    sa = scene_alpha(t, "S5", 0.45, 0.45)
    chrome(c, t, "S5", 1.0)
    ev = 1 - ease_in_out(prog(t, 58.2, 58.7))
    if t < 58.7:
        ca = case("ambiguous")
        cand = ca["candidates"][0]
        x, y, bw, bh = cand["bbox_xywh_reference"]
        cx, cy = x + bw / 2, y + bh / 2
        zw = lerp(240, 165, ease_in_out(prog(t, 53.2, 56.0)))
        a0 = ease_out(prog(t, 53.2, 53.9)) * ev
        draw_text(c, "How evidence becomes a decision", 120, 136, "display", 46, WHITE, a0)
        for i, (name, lab) in enumerate((("aligned", "Aligned inspection · case 03"), ("heatmap", "Anomaly map · uncalibrated"))):
            px = 120 + i * 560
            img = zoom_crop("ambiguous", name, cx, cy, zw, 520, 390)
            a = ease_out(prog(t, 53.4 + i * 0.2, 54.1 + i * 0.2)) * ev
            panel_image(c, img, px, 290, alpha=a)
            label(c, lab, px, 254, MUTED, a)
            s = 520 / zw
            brackets(c, px + 260 - bw * s / 2 - 10, 290 + 195 - bh * s / 2 - 10, bw * s + 20, bh * s + 20,
                     DECISION["HUMAN_REVIEW"], 18, 3, a, ease_out(prog(t, 54.6, 55.1)))
        label(c, f"candidate 0 · first-pass peak {cand['peak']:.2f} · {cand['area_px']} px · status {cand['status']}", 120, 710,
              DECISION["HUMAN_REVIEW"], ease_out(prog(t, 55.0, 55.5)) * ev)
        draw_text(c, "Scores rank regions; they are heuristics, not probabilities.", 120, 750, "regular", 24, MUTED, ease_out(prog(t, 55.3, 55.9)) * ev)
        cards = [
            ("C0", "Strong evidence → confirmed", "peak ≥ 0.75 and area ≥ 400 px, no lighting shift (the missing part: 0.94, 1831 px)", WHITE, 54.2),
            ("C1", "Weaker evidence → re-inspect", "structure view ≥ 0.70 and at least one other independent view must reproduce it", BLUE, 55.2),
            ("T2", "Still unresolved → HUMAN_REVIEW", "no guess: the case goes to a person, with the evidence attached", DECISION["HUMAN_REVIEW"], 56.3),
        ]
        for i, (rule, head, body, col, t0) in enumerate(cards):
            a = ease_out(prog(t, t0, t0 + 0.55)) * ev
            yy = 290 + i * 150
            rect(c, 1260, yy, 540, 128, col, 1.6, a, radius=12)
            if rule == "T2":
                rect(c, 1260, yy, 540, 128, col, alpha=a * 0.14, fill=True, radius=12)
            draw_text(c, rule, 1284, yy + 20, "mono_medium", 22, col, a)
            draw_text(c, head, 1346, yy + 18, "semibold", 24, WHITE, a)
            draw_paragraph(c, body, 1346, yy + 58, 430, "regular", 20, MUTED, a, leading=1.32)
    if t >= 58.5:
        aa = ease_out(prog(t, 58.6, 59.2))
        draw_text(c, "Deployed architecture", 120, 136, "display", 46, WHITE, aa)
        draw_text(c, "AWS SAM template · ap-south-1", 120, 196, "regular", 24, MUTED, aa)
        for i, (k, title, ls, x, w, col) in enumerate(ARCH):
            a = ease_out(prog(t, 59.0 + i * 0.35, 59.5 + i * 0.35))
            rect(c, x, ARCH_Y, w, ARCH_H, col, 1.8, a, radius=12)
            draw_text(c, title, x + 22, ARCH_Y + 18, "semibold", 26, WHITE, a)
            for j, ln in enumerate(ls):
                draw_text(c, ln, x + 22, ARCH_Y + 58 + j * 27, "regular", 19, MUTED, a)
            if i > 0:
                px0 = ARCH[i - 1][3] + ARCH[i - 1][4]
                arrow(c, px0 + 6, ARCH_Y + 40, x - 8, ARCH_Y + 40, WHITE, 1.8, a, 9, ease_out(prog(t, 59.0 + i * 0.35, 59.4 + i * 0.35)))
                if 59.4 + i * 0.35 < t < 60.2 + i * 0.35:
                    q = prog(t, 59.4 + i * 0.35, 60.1 + i * 0.35)
                    glow_dot(c, lerp(px0 + 6, x - 8, ease_in_out(q)), ARCH_Y + 40, BLUE, 4)
        # response path
        ra = ease_out(prog(t, 60.6, 61.2))
        ry = ARCH_Y + ARCH_H + 34
        arrow(c, 1590, ARCH_Y + ARCH_H + 6, 1590, ry, BLUE, 1.6, ra, 0, 1.0)
        arrow(c, 1590, ry, 290, ry, BLUE, 1.8, ra, 9, ease_out(prog(t, 60.6, 61.5)))
        arrow(c, 290, ry, 290, ARCH_Y + ARCH_H + 8, BLUE, 1.8, ra * ease_out(prog(t, 61.3, 61.6)), 9)
        draw_text(c, "JSON response: decision · termination rule · reasons · step trace · evidence images",
                  940, ry + 14, "regular", 21, TEXT, ra, anchor="ma")
        cw = ease_out(prog(t, 61.0, 61.5))
        label(c, "+ CloudWatch Logs: request metadata only, 7-day retention", 970, ARCH_Y - 40, FAINT, cw)
        # terminal
        mode, out, cap = smoke()
        ta = ease_out(prog(t, 61.4, 62.0))
        tx, ty, tw, th = 120, 520, 1680, 452
        rect(c, tx, ty, tw, th, (13, 14, 16), alpha=ta, fill=True, radius=12)
        rect(c, tx, ty, tw, th, LINE, 1.4, ta, radius=12)
        for i, col in enumerate(((255, 95, 87), (254, 188, 46), (40, 200, 64))):
            circle(c, tx + 24 + i * 20, ty + 22, 6, col, alpha=ta * 0.8, fill=True)
        label(c, cap, tx + tw - 22, ty + 13, FAINT, ta, size=15, anchor="ra")
        url = "http://127.0.0.1:8765" if mode == "local" else "$LOOPCHECK_API_URL"
        cmd = f"$ python -m loopcheck.client --url {url} --case data/demo_cases/02_missing_part --expect DEFECT"
        keep = [ln for ln in out if any(k in ln for k in ('"health_status"', '"health_opencv_version"', '"inspect_opencv_version"',
                '"decision"', '"termination_rule"', '"agent_tool_calls"', '"server_seconds"', "SMOKE TEST"))]
        seq = [ln for ln in out if '"R' in ln]
        body = keep[:6] + ['  "agent_tool_sequence": [' + ", ".join(s.strip().rstrip(",") for s in seq) + "],"] + keep[6:]
        shown = [cmd] + body
        for i, ln in enumerate(shown):
            t0 = 62.0 + i * 0.36
            if t < t0:
                break
            n = int(len(ln) * ease_out(prog(t, t0, t0 + (0.55 if i == 0 else 0.22))))
            col = TEXT
            if '"DEFECT"' in ln or '"T1"' in ln:
                col = (255, 140, 140)
            if '"ok"' in ln or "5.0.0" in ln or "PASS" in ln:
                col = (140, 225, 160)
            if i == 0:
                col = (150, 205, 255)
            draw_text(c, ln[:n], tx + 30, ty + 52 + i * 33.5, "mono", 20, col, ta)
    return fade(c, 1 - sa)


# ============================================================================ S6 transparency + limits
def s6(t: float) -> Image.Image:
    c = new_frame()
    sa = scene_alpha(t, "S6", 0.45, 0.45)
    chrome(c, t, "S6", 1.0)
    part1 = 1 - ease_in_out(prog(t, 72.2, 72.7))
    if t < 72.7:
        a = ease_out(prog(t, 67.2, 67.9)) * part1
        draw_text(c, "Every decision ships with its evidence.", 120, 150, "display", 52, WHITE, a)
        draw_text(c, "Intermediate images, the rule behind each step, and a machine-readable report.", 122, 226, "regular", 27, MUTED, a)
        cw, chh, gap, y = 312, 234, 30, 360
        trace = load("app/missing_trace.jpg")
        tb = capture()["missing_trace"]["boxes"]["steps"]
        tx0, ty0 = tb[0]["x"] - 20, tb[0]["y"] - 20
        cards = [("aligned", "Aligned"), ("heatmap", "Anomaly map"), ("overlay", "Annotated overlay"), ("trace", "Agent trace"), ("json", "JSON report")]
        for i, (k, lab) in enumerate(cards):
            x = 120 + i * (cw + gap)
            ai = ease_out(prog(t, 67.6 + i * 0.22, 68.2 + i * 0.22)) * part1
            if k in ("aligned", "heatmap", "overlay"):
                img = sized("missing", k, cw, chh)
            elif k == "trace":
                img = crop_to(trace, (tx0, ty0, tx0 + 1000, ty0 + 1000 * chh / cw), (cw, chh))
            else:
                img = Image.new("RGBA", (cw, chh), (13, 14, 16, 255))
                cm = case("missing")
                js = ["{", f' "decision": "{cm["decision"]}",', f' "termination_rule": "{cm["termination_rule"]}",',
                      ' "reasons": [...],', ' "report": {', '   "steps": [...],', '   "candidates": [...]},', ' "disclaimer": "..."', "}"]
                for j, ln in enumerate(js):
                    draw_text(img, ln, 16, 18 + j * 25, "mono", 16, (150, 205, 255) if j in (1, 2) else TEXT, 1.0)
            panel_image(c, img, x, y + (1 - ai) * 18, alpha=ai)
            label(c, lab, x, y + chh + 22, MUTED, ai)
        draw_text(c, "Shown: case 02 (Missing part), as returned by the real service.", 120, 690, "regular", 21, FAINT,
                  ease_out(prog(t, 68.8, 69.4)) * part1)
    if t >= 72.5:
        a = ease_out(prog(t, 72.6, 73.3))
        draw_text(c, "Held-out synthetic evaluation", 120, 150, "display", 52, WHITE, a)
        stats = [("180", "held-out synthetic cases"), ("0", "errors among automatic decisions"), ("36%", "routed to HUMAN_REVIEW")]
        for i, (num, lab) in enumerate(stats):
            ai = ease_out(prog(t, 73.0 + i * 0.3, 73.6 + i * 0.3))
            x = 120 + i * 520
            draw_text(c, num, x - 4, 250 + (1 - ai) * 16, "display", 112, WHITE if i != 2 else DECISION["HUMAN_REVIEW"], ai)
            draw_text(c, lab, x, 390, "regular", 26, TEXT, ai)
        label(c, "Source: results/d3/test/metrics.json · thresholds frozen on the development split before the test run",
              120, 452, FAINT, ease_out(prog(t, 74.0, 74.5)), size=15)
        q = ease_out(prog(t, 74.6, 75.3))
        rect(c, 120, 540, 1680, 360, RED, 1.6, q, radius=14)
        rect(c, 120, 540, 1680, 360, RED, alpha=q * 0.06, fill=True, radius=14)
        rect(c, 120, 540, 8, 360, RED, alpha=q, fill=True, radius=3)
        draw_text(c, "Research prototype • Synthetic-data evaluation", 168, 586, "display", 48, WHITE, q)
        for j, ln in enumerate(("Synthetic validation does not establish real-world inspection performance.",
                                "Anomaly scores are uncalibrated heuristics, not probabilities.",
                                "Not certified for production use. HUMAN_REVIEW is a deliberate outcome, not a failure.")):
            aj = ease_out(prog(t, 75.2 + j * 0.35, 75.8 + j * 0.35))
            draw_text(c, ln, 170, 680 + j * 56, "regular", 31, TEXT, aj)
    return fade(c, 1 - sa)


# ============================================================================ S7 closing
def s7(t: float) -> Image.Image:
    box = macro_camera(t, 80.0, 88.0, 1300, 1200, (380, 225), (400, 235))
    c = macro_frame(box)
    c = fade(c, 0.5)
    x0, y0, x1, y1 = missing_box_insp()
    (fx0, fy0), (fx1, fy1) = to_frame(box, x0, y0), to_frame(box, x1, y1)
    brackets(c, fx0 - 16, fy0 - 16, fx1 - fx0 + 32, fy1 - fy0 + 32, RED, 26, 3, 0.55, 1.0)
    p = ease_out(prog(t, 80.6, 81.6))
    draw_text(c, "LoopCheck", W / 2, 380, "display", 150, WHITE, p, anchor="ma", dy=(1 - p) * 24)
    rw = 130 * ease_out(prog(t, 81.0, 81.8))
    rect(c, W / 2 - rw / 2, 590, rw, 4, RED, alpha=p, fill=True)
    q = ease_out(prog(t, 81.4, 82.3))
    draw_text(c, "Visual evidence. Explicit decisions. Inspectable execution.", W / 2, 628, "display_light", 44, WHITE, q, anchor="ma")
    r = ease_out(prog(t, 82.2, 82.9))
    label(c, "Research prototype • Synthetic-data evaluation  ·  OpenCV 5  ·  AWS Lambda", W / 2, 960, MUTED, r, anchor="ma")
    c = fade(c, 1 - ease_out(prog(t, 80.0, 80.8)))
    return fade(c, ease_in_out(prog(t, 86.4, 87.7)))


# ============================================================================ dispatcher
RENDER = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6, "S7": s7}


def frame(t: float) -> Image.Image:
    if t >= DURATION - 0.3:
        return black_frame()
    for s in SCENES:
        if s["start"] <= t < s["end"]:
            return RENDER[s["id"]](t)
    return black_frame()
