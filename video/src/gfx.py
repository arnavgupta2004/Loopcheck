"""Drawing toolkit for the LoopCheck film: palette, type, easing, anti-aliased vectors, images.

Everything is deterministic (no clocks, no unseeded randomness). Frames are PIL RGBA
1920x1080 canvases. Vector shapes are rasterised with OpenCV's anti-aliased primitives into a
coverage mask and composited with PIL, so edges stay clean at any sub-pixel position.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

VIDEO = Path(__file__).resolve().parents[1]
ASSETS = VIDEO / "assets"
W, H = 1920, 1080

# ----------------------------------------------------------------------------- palette
BG = (10, 11, 13)
BG_LIFT = (19, 21, 25)
PANEL = (20, 22, 26)
LINE = (44, 48, 55)
GRID = (255, 255, 255, 9)
WHITE = (242, 243, 245)
TEXT = (214, 217, 222)
MUTED = (138, 144, 153)
FAINT = (88, 93, 101)
RED = (229, 72, 77)
BLUE = (88, 166, 222)
BLUE_DIM = (52, 98, 133)
# decision colours exactly as the app renders them (app/streamlit_app.py COLOURS)
DECISION = {"APPROVE": (27, 127, 59), "DEFECT": (198, 40, 40), "HUMAN_REVIEW": (224, 123, 0)}

FONT_FILES = {
    "display": "InterDisplay-SemiBold.otf", "display_light": "InterDisplay-Light.otf",
    "regular": "Inter-Regular.otf", "medium": "Inter-Medium.otf", "semibold": "Inter-SemiBold.otf",
    "mono": "JetBrainsMono-Regular.ttf", "mono_medium": "JetBrainsMono-Medium.ttf",
}


@lru_cache(maxsize=None)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ASSETS / "fonts" / FONT_FILES[kind]), size)


# ----------------------------------------------------------------------------- timing
def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return a if x < a else b if x > b else x


def prog(t: float, a: float, b: float) -> float:
    return clamp((t - a) / (b - a)) if b > a else float(t >= b)


def ease_out(x: float) -> float:          # cubic
    return 1 - (1 - x) ** 3


def ease_in_out(x: float) -> float:       # cubic
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def ease_expo(x: float) -> float:
    return 1.0 if x >= 1 else 1 - 2 ** (-10 * x)


def lerp(a, b, x):
    if isinstance(a, (tuple, list)):
        return type(a)(lerp(u, v, x) for u, v in zip(a, b))
    return a + (b - a) * x


def window(t: float, a: float, b: float, fade_in: float = 0.5, fade_out: float = 0.5) -> float:
    """1 inside [a, b] with eased fades at both ends; 0 outside."""
    if t < a or t > b:
        return 0.0
    return ease_in_out(prog(t, a, a + fade_in)) * (1 - ease_in_out(prog(t, b - fade_out, b)))


# ----------------------------------------------------------------------------- canvas
@lru_cache(maxsize=1)
def _background() -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    # soft lift towards the upper-left third, vignette towards the corners
    d = np.sqrt(((xx - W * 0.38) / W) ** 2 + ((yy - H * 0.30) / H) ** 2)
    lift = np.clip(1 - d * 1.6, 0, 1) ** 2
    base = np.array(BG, np.float32)[None, None] + (np.array(BG_LIFT, np.float32) - np.array(BG, np.float32))[None, None] * lift[..., None]
    vig = np.clip(1 - (((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H / 2) / (H * 0.70)) ** 2) * 0.35, 0.55, 1)
    img = Image.fromarray(np.clip(base * vig[..., None], 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    for x in range(0, W, 60):
        g.line([(x, 0), (x, H)], fill=GRID)
    for y in range(0, H, 60):
        g.line([(0, y), (W, y)], fill=GRID)
    return Image.alpha_composite(img, grid)


def new_frame() -> Image.Image:
    return _background().copy()


def black_frame() -> Image.Image:
    return Image.new("RGBA", (W, H), (0, 0, 0, 255))


def fade(canvas: Image.Image, amount: float, color=(0, 0, 0)) -> Image.Image:
    """Blend the whole frame towards `color` by `amount` (0..1)."""
    if amount <= 0:
        return canvas
    over = Image.new("RGBA", canvas.size, color + (int(255 * clamp(amount)),))
    return Image.alpha_composite(canvas, over)


# ----------------------------------------------------------------------------- text
@lru_cache(maxsize=4096)
def text_image(text: str, kind: str, size: int, color: tuple, tracking: float = 0.0) -> Image.Image:
    """Tight RGBA image of a single line. `tracking` is extra px between glyphs."""
    f = font(kind, size)
    asc, desc = f.getmetrics()
    if tracking == 0:
        w = int(math.ceil(f.getlength(text))) + 4
    else:
        w = int(math.ceil(sum(f.getlength(c) + tracking for c in text) - tracking)) + 4
    img = Image.new("RGBA", (max(w, 1), asc + desc + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if tracking == 0:
        d.text((2, 2), text, font=f, fill=tuple(color) + (255,))
    else:
        x = 2.0
        for c in text:
            d.text((x, 2), c, font=f, fill=tuple(color) + (255,))
            x += f.getlength(c) + tracking
    return img


def text_width(text: str, kind: str, size: int, tracking: float = 0.0) -> float:
    f = font(kind, size)
    return f.getlength(text) if tracking == 0 else sum(f.getlength(c) + tracking for c in text) - tracking


def with_alpha(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return img
    a = img.getchannel("A").point(lambda v: int(v * clamp(alpha)))
    out = img.copy()
    out.putalpha(a)
    return out


def paste(canvas: Image.Image, img: Image.Image, x: float, y: float, alpha: float = 1.0) -> None:
    """Alpha-composite `img` with its top-left at (x, y). Sub-pixel x/y are rounded (text is static)."""
    if alpha <= 0.002:
        return
    canvas.alpha_composite(with_alpha(img, alpha), (int(round(x)), int(round(y))))


def draw_text(canvas, text, x, y, kind="regular", size=32, color=TEXT, alpha=1.0, tracking=0.0, anchor="la", dy=0.0):
    """anchor: l/m/r + a(top)/m(middle)/b(baseline-ish bottom)."""
    img = text_image(text, kind, size, tuple(color), tracking)
    w, h = img.size
    ox = {"l": 0, "m": -w / 2, "r": -w}[anchor[0]]
    oy = {"a": 0, "m": -h / 2, "b": -h}[anchor[1]]
    paste(canvas, img, x + ox, y + oy + dy, alpha)
    return w, h


def wrap(text: str, kind: str, size: int, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if text_width(trial, kind, size) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_paragraph(canvas, text, x, y, max_w, kind="regular", size=30, color=TEXT, alpha=1.0, leading=1.38, stagger=0.0, t_local=None):
    """Wrapped paragraph; with `stagger` and `t_local`, lines rise in one after another."""
    yy = y
    for i, line in enumerate(wrap(text, kind, size, max_w)):
        a, dy = alpha, 0.0
        if stagger and t_local is not None:
            p = ease_out(prog(t_local, i * stagger, i * stagger + 0.6))
            a, dy = alpha * p, (1 - p) * 14
        draw_text(canvas, line, x, yy, kind, size, color, a, dy=dy)
        yy += size * leading
    return yy


def label(canvas, text, x, y, color=MUTED, alpha=1.0, size=17, anchor="la"):
    """Uppercase mono micro-label with tracking (the film's technical voice)."""
    return draw_text(canvas, text.upper(), x, y, "mono_medium", size, color, alpha, tracking=2.2, anchor=anchor)


# ----------------------------------------------------------------------------- vectors
def _mask_draw(fn, bbox=None) -> tuple[np.ndarray, tuple[int, int]]:
    x0, y0, x1, y1 = bbox or (0, 0, W, H)
    x0, y0 = max(int(x0), 0), max(int(y0), 0)
    x1, y1 = min(int(math.ceil(x1)), W), min(int(math.ceil(y1)), H)
    m = np.zeros((max(y1 - y0, 1), max(x1 - x0, 1)), np.uint8)
    fn(m, x0, y0)
    return m, (x0, y0)


def _composite_mask(canvas, mask, origin, color, alpha):
    if alpha <= 0.002 or not mask.any():
        return
    if alpha < 1:
        mask = (mask.astype(np.float32) * alpha).astype(np.uint8)
    solid = Image.new("RGBA", (mask.shape[1], mask.shape[0]), tuple(color[:3]) + (255,))
    solid.putalpha(Image.fromarray(mask, "L"))
    canvas.alpha_composite(solid, origin)


SHIFT = 4  # cv2 sub-pixel bits
_S = 1 << SHIFT


def _p(x, y, ox, oy):
    return (int(round((x - ox) * _S)), int(round((y - oy) * _S)))


def lines(canvas, segments, color, width=2.0, alpha=1.0):
    """segments: list of ((x0, y0), (x1, y1))."""
    if not segments:
        return
    xs = [p[0] for s in segments for p in s]
    ys = [p[1] for s in segments for p in s]
    pad = width + 4

    def fn(m, ox, oy):
        for a, b in segments:
            cv2.line(m, _p(*a, ox, oy), _p(*b, ox, oy), 255, max(int(round(width)), 1), cv2.LINE_AA, SHIFT)
    m, o = _mask_draw(fn, (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))
    _composite_mask(canvas, m, o, color, alpha)


def polyline(canvas, pts, color, width=2.0, alpha=1.0, closed=False):
    segs = list(zip(pts[:-1], pts[1:]))
    if closed:
        segs.append((pts[-1], pts[0]))
    lines(canvas, segs, color, width, alpha)


def rect(canvas, x, y, w, h, color, width=2.0, alpha=1.0, fill=False, radius=0.0):
    pad = width + 4

    def fn(m, ox, oy):
        if radius > 0:
            _rounded(m, x - ox, y - oy, w, h, radius, fill, width)
        elif fill:
            cv2.rectangle(m, _p(x, y, ox, oy), _p(x + w, y + h, ox, oy), 255, -1, cv2.LINE_AA, SHIFT)
        else:
            cv2.rectangle(m, _p(x, y, ox, oy), _p(x + w, y + h, ox, oy), 255, max(int(round(width)), 1), cv2.LINE_AA, SHIFT)
    m, o = _mask_draw(fn, (x - pad, y - pad, x + w + pad, y + h + pad))
    _composite_mask(canvas, m, o, color, alpha)


def _rounded(m, x, y, w, h, r, fill, width):
    r = min(r, w / 2, h / 2)
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for k in range(0, 91, 6):
            a = math.radians(a0 + k)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    arr = np.array([[int(round(px * _S)), int(round(py * _S))] for px, py in pts], np.int32)
    if fill:
        cv2.fillPoly(m, [arr], 255, cv2.LINE_AA, SHIFT)
    else:
        cv2.polylines(m, [arr], True, 255, max(int(round(width)), 1), cv2.LINE_AA, SHIFT)


def circle(canvas, cx, cy, r, color, width=2.0, alpha=1.0, fill=False):
    pad = r + width + 4

    def fn(m, ox, oy):
        cv2.circle(m, _p(cx, cy, ox, oy), int(round(r * _S)), 255, -1 if fill else max(int(round(width)), 1), cv2.LINE_AA, SHIFT)
    m, o = _mask_draw(fn, (cx - pad, cy - pad, cx + pad, cy + pad))
    _composite_mask(canvas, m, o, color, alpha)


def brackets(canvas, x, y, w, h, color, arm=18.0, width=3.0, alpha=1.0, grow=1.0):
    """Viewfinder-style corner brackets; `grow` 0..1 animates the arms."""
    a = arm * clamp(grow)
    segs = []
    for cx, cy, sx, sy in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        segs += [((cx, cy), (cx + sx * a, cy)), ((cx, cy), (cx, cy + sy * a))]
    lines(canvas, segs, color, width, alpha)


def arrow(canvas, x0, y0, x1, y1, color, width=2.0, alpha=1.0, head=10.0, progress=1.0):
    p = clamp(progress)
    if p <= 0:
        return
    xe, ye = x0 + (x1 - x0) * p, y0 + (y1 - y0) * p
    segs = [((x0, y0), (xe, ye))]
    if p > 0.98:
        ang = math.atan2(y1 - y0, x1 - x0)
        for s in (-1, 1):
            segs.append(((xe, ye), (xe - head * math.cos(ang + s * 0.45), ye - head * math.sin(ang + s * 0.45))))
    lines(canvas, segs, color, width, alpha)


def glow_dot(canvas, x, y, color, r=5.0, alpha=1.0):
    circle(canvas, x, y, r * 3.2, color, alpha=alpha * 0.10, fill=True)
    circle(canvas, x, y, r * 1.8, color, alpha=alpha * 0.22, fill=True)
    circle(canvas, x, y, r, color, alpha=alpha, fill=True)


# ----------------------------------------------------------------------------- images
@lru_cache(maxsize=64)
def load(path: str) -> Image.Image:
    return Image.open(ASSETS / path).convert("RGBA")


def crop_to(src: Image.Image, box: tuple[float, float, float, float], size: tuple[int, int], resample=Image.BICUBIC) -> Image.Image:
    """Resample the float source box (x0, y0, x1, y1) to `size`: sub-pixel accurate camera moves."""
    return src.resize((max(int(size[0]), 1), max(int(size[1]), 1)), resample, box=box, reducing_gap=None)


def fit(src: Image.Image, w: int, h: int, resample=Image.LANCZOS) -> Image.Image:
    return src.resize((int(w), int(h)), resample)


@lru_cache(maxsize=64)
def round_mask(w: int, h: int, r: int) -> Image.Image:
    s = 3
    m = Image.new("L", (w * s, h * s), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * s - 1, h * s - 1), r * s, fill=255)
    return m.resize((w, h), Image.LANCZOS)


@lru_cache(maxsize=64)
def shadow(w: int, h: int, r: int, blur: int = 28, strength: int = 150) -> Image.Image:
    pad = blur * 3
    m = Image.new("L", (w + pad * 2, h + pad * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad + blur // 3, pad + w, pad + h + blur // 3), r, fill=strength)
    m = m.filter(ImageFilter.GaussianBlur(blur))
    img = Image.new("RGBA", m.size, (0, 0, 0, 255))
    img.putalpha(m)
    return img


def panel_image(canvas, img: Image.Image, x: float, y: float, radius: int = 12, alpha: float = 1.0, border=LINE, shadow_on=True):
    """Image with rounded corners, hairline border and soft shadow."""
    w, h = img.size
    if shadow_on:
        sh = shadow(w, h, radius)
        pad = (sh.size[0] - w) // 2
        paste(canvas, sh, x - pad, y - pad, alpha * 0.9)
    m = round_mask(w, h, radius)
    if alpha < 1:
        m = m.point(lambda v: int(v * alpha))
    im = img.convert("RGBA").copy()
    im.putalpha(Image.fromarray(np.minimum(np.array(m), np.array(img.getchannel("A")) if img.mode == "RGBA" else np.array(m)), "L"))
    canvas.alpha_composite(im, (int(round(x)), int(round(y))))
    if border:
        rect(canvas, x + 0.5, y + 0.5, w - 1, h - 1, border, 1.4, alpha, radius=radius)


def chip(canvas, text, x, y, color, alpha=1.0, size=18, filled=False, text_color=None, pad_x=14, pad_y=8, anchor="l"):
    """Rounded tag. Returns its width."""
    tw = text_width(text.upper(), "mono_medium", size, 1.6)
    w, h = tw + pad_x * 2, size + pad_y * 2 + 2
    if anchor == "m":
        x -= w / 2
    elif anchor == "r":
        x -= w
    if filled:
        rect(canvas, x, y, w, h, color, alpha=alpha, fill=True, radius=h / 2)
    else:
        rect(canvas, x, y, w, h, color, 1.6, alpha, radius=h / 2)
    draw_text(canvas, text.upper(), x + pad_x, y + pad_y - 1, "mono_medium", size, text_color or (WHITE if filled else color), alpha, tracking=1.6)
    return w


def decision_badge(canvas, decision: str, rule: str, x: float, y: float, alpha=1.0, size=40, anchor="l"):
    """Decision pill in the app's own decision colours."""
    col = DECISION[decision]
    tw = text_width(decision, "display", size)
    rw = text_width(f"rule {rule}", "mono_medium", int(size * 0.5), 1.0)
    w, h = tw + rw + 70, size * 1.75
    if anchor == "m":
        x -= w / 2
    rect(canvas, x, y, w, h, col, alpha=alpha, fill=True, radius=10)
    draw_text(canvas, decision, x + 24, y + h / 2, "display", size, WHITE, alpha, anchor="lm", dy=-2)
    draw_text(canvas, f"rule {rule}", x + 24 + tw + 22, y + h / 2, "mono_medium", int(size * 0.5), (255, 255, 255), alpha * 0.9, tracking=1.0, anchor="lm")
    return w, h


def cursor(canvas, x, y, alpha=1.0, pressed=0.0):
    """macOS-like arrow pointer drawn as vectors (presentation aid over real screenshots)."""
    s = 1.0 - 0.12 * pressed
    pts = [(0, 0), (0, 25), (6.5, 19), (11, 29.5), (15, 27.8), (10.6, 17.6), (19, 17.6)]
    P = [(x + px * s, y + py * s) for px, py in pts]
    sh = [(px + 1.5, py + 2.5) for px, py in P]

    def poly(m, ox, oy, pp):
        arr = np.array([_p(px, py, ox, oy) for px, py in pp], np.int32)
        cv2.fillPoly(m, [arr], 255, cv2.LINE_AA, SHIFT)
    m, o = _mask_draw(lambda m, ox, oy: poly(m, ox, oy, sh), (x - 4, y - 4, x + 26, y + 36))
    _composite_mask(canvas, cv2.GaussianBlur(m, (5, 5), 0), o, (0, 0, 0), alpha * 0.45)
    m, o = _mask_draw(lambda m, ox, oy: poly(m, ox, oy, P), (x - 4, y - 4, x + 26, y + 36))
    _composite_mask(canvas, m, o, (12, 12, 14), alpha)
    inner = [(x + 1.6 * s + (px - x) * 0.80, y + 3.2 * s + (py - y) * 0.80) for px, py in P]
    m, o = _mask_draw(lambda m, ox, oy: poly(m, ox, oy, inner), (x - 4, y - 4, x + 26, y + 36))
    _composite_mask(canvas, m, o, (250, 250, 250), alpha)


def click_ring(canvas, x, y, p, color=WHITE):
    """Expanding ring for a click, p in 0..1."""
    if 0 < p < 1:
        circle(canvas, x, y, 8 + 26 * ease_out(p), color, 2.0, (1 - p) * 0.8)


def to_rgb_bytes(canvas: Image.Image) -> bytes:
    return canvas.convert("RGB").tobytes()
