"""Deterministic synthetic tray scenes with known ground truth.

    uv run python -m loopcheck.synth --out data/synthetic/dev --seed 1000 --per-type 8

Each case directory holds reference.png and inspection.png; manifest.json records the
case type, expected status, defect boxes (reference px, [x, y, w, h]) and the true
homography H (reference px -> inspection px). The scene is rendered on a canvas with
a margin so that perspective changes reveal real background instead of padding.
Same seed => byte-identical PNGs and manifest.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np

W, H = 800, 600
MARGIN = 80
MARKER_SIZE = 56
TRAY = (60, 50, 740, 550)  # x1, y1, x2, y2 in reference px
SLOT_COLS, SLOT_ROWS = 4, 3
SLOT_AREA = (145, 70, 655, 530)

NOMINAL_TYPES = ["identical", "lighting", "geometric", "blur_noise", "combined_nominal"]
DEFECT_TYPES = ["missing", "shifted", "foreign_object", "missing_hard", "subtle_foreign"]
REGFAIL_TYPES = ["unrelated_scene", "low_overlap", "heavy_blur"]
# D2.1 additions. Appended (never inserted) so the per-type seeds of older types, which
# derive from CASE_TYPES.index(), are unchanged and older splits regenerate identically.
NOMINAL_TYPES_V2 = ["gamma_lighting"]
DEFECT_TYPES_V2 = ["missing_near_marker", "foreign_near_marker"]
DIFFERENT_TRAY_TYPES = ["different_tray"]  # same marker ids/positions, different contents
NOMINAL_TYPES = NOMINAL_TYPES + NOMINAL_TYPES_V2
DEFECT_TYPES = DEFECT_TYPES + DEFECT_TYPES_V2
CASE_TYPES = NOMINAL_TYPES[:5] + DEFECT_TYPES[:5] + REGFAIL_TYPES + NOMINAL_TYPES_V2 + DEFECT_TYPES_V2 + DIFFERENT_TRAY_TYPES
D2_CASE_TYPES = CASE_TYPES[:13]  # the case set used for the D2 splits (seeds 1000/2000)
MARKER_FORCED_TYPES = set(NOMINAL_TYPES_V2 + DEFECT_TYPES_V2 + DIFFERENT_TRAY_TYPES)
QUIET_ZONE = 10  # white margin around each rendered marker (px)
MARKER_SPOTS = [  # top-left pixel of markers 0..3 in reference px
    (TRAY[0] + 14, TRAY[1] + 14),
    (TRAY[2] - 14 - MARKER_SIZE, TRAY[1] + 14),
    (TRAY[2] - 14 - MARKER_SIZE, TRAY[3] - 14 - MARKER_SIZE),
    (TRAY[0] + 14, TRAY[3] - 14 - MARKER_SIZE),
]
NEAR_MARKER_PX = 40.0  # a GT box within this distance of a marker quiet zone counts as "near"
KINDS = ("disk", "chip", "nut", "cylinder", "bolt", "resistor")


@dataclass
class Component:
    kind: str
    cx: float
    cy: float
    size: float
    angle: float
    color: tuple[int, int, int]
    accent: tuple[int, int, int]


@dataclass
class Scene:
    seed: int
    bg_color: tuple[int, int, int]
    tray_color: tuple[int, int, int]
    slot_color: tuple[int, int, int]
    markers: bool
    components: list[Component] = field(default_factory=list)
    foreign: list[Component] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Scene construction
# --------------------------------------------------------------------------- #
def _color(rng, lo=30, hi=230) -> tuple[int, int, int]:
    return tuple(int(v) for v in rng.integers(lo, hi, 3))


def _far_color(rng, avoid, min_dist=90) -> tuple[int, int, int]:
    for _ in range(50):
        c = _color(rng, 20, 245)
        if np.linalg.norm(np.subtract(c, avoid)) >= min_dist:
            return c
    return tuple(255 - v for v in avoid)


def slot_rects() -> list[tuple[int, int, int, int]]:
    x1, y1, x2, y2 = SLOT_AREA
    pw, ph = (x2 - x1) / SLOT_COLS, (y2 - y1) / SLOT_ROWS
    return [
        (int(x1 + c * pw + 8), int(y1 + r * ph + 8), int(x1 + (c + 1) * pw - 8), int(y1 + (r + 1) * ph - 8))
        for r in range(SLOT_ROWS)
        for c in range(SLOT_COLS)
    ]


def make_scene(seed: int, markers: bool) -> Scene:
    rng = np.random.default_rng(seed)
    tray = _color(rng, 140, 215)
    slot = tuple(max(0, v - int(rng.integers(30, 60))) for v in tray)
    scene = Scene(seed, _color(rng, 20, 90), tray, slot, markers)
    for x1, y1, x2, y2 in slot_rects():
        if rng.random() < 0.08:
            continue  # empty slot
        scene.components.append(
            Component(
                kind=KINDS[int(rng.integers(len(KINDS)))],
                cx=(x1 + x2) / 2 + rng.uniform(-6, 6),
                cy=(y1 + y2) / 2 + rng.uniform(-6, 6),
                size=float(rng.uniform(26, 38)),
                angle=float(rng.uniform(0, 180)),
                color=_far_color(rng, slot),
                accent=_color(rng, 0, 255),
            )
        )
    return scene


def _rot(pts: np.ndarray, angle_deg: float, cx: float, cy: float) -> np.ndarray:
    a = math.radians(angle_deg)
    r = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    return (pts @ r.T + [cx, cy]).round().astype(np.int32)


def draw_component(img: np.ndarray, c: Component, off: int, mask: bool = False) -> None:
    col = (255,) * img.shape[2] if mask else c.color
    acc = (255,) * img.shape[2] if mask else c.accent
    dark = (255,) * img.shape[2] if mask else tuple(int(v * 0.45) for v in c.color)
    cx, cy, s = c.cx + off, c.cy + off, c.size
    aa = cv2.LINE_8 if mask else cv2.LINE_AA
    if c.kind == "disk":
        cv2.circle(img, (round(cx), round(cy)), round(s), col, -1, aa)
        cv2.circle(img, (round(cx), round(cy)), round(s * 0.45), dark, -1, aa)
        cv2.circle(img, (round(cx - s * 0.5), round(cy - s * 0.5)), max(2, round(s * 0.12)), acc, -1, aa)
    elif c.kind == "chip":
        body = _rot(np.array([[-s, -0.6 * s], [s, -0.6 * s], [s, 0.6 * s], [-s, 0.6 * s]]), c.angle, cx, cy)
        for t in np.linspace(-0.8, 0.8, 5):
            for side in (-1, 1):
                pin = _rot(np.array([[t * s, side * 0.6 * s], [t * s, side * 0.85 * s]]), c.angle, cx, cy)
                cv2.line(img, tuple(pin[0]), tuple(pin[1]), acc, 3, aa)
        cv2.fillConvexPoly(img, body, col, aa)
        dot = _rot(np.array([[-0.7 * s, -0.3 * s]]), c.angle, cx, cy)[0]
        cv2.circle(img, tuple(dot), max(2, round(s * 0.1)), acc, -1, aa)
    elif c.kind == "nut":
        hexagon = _rot(np.array([[s * math.cos(k * math.pi / 3), s * math.sin(k * math.pi / 3)] for k in range(6)]), c.angle, cx, cy)
        cv2.fillConvexPoly(img, hexagon, col, aa)
        cv2.circle(img, (round(cx), round(cy)), round(s * 0.42), dark, -1, aa)
        cv2.polylines(img, [hexagon], True, acc, 2, aa)
    elif c.kind == "cylinder":
        cv2.ellipse(img, (round(cx), round(cy)), (round(s), round(s * 0.55)), c.angle, 0, 360, col, -1, aa)
        for t in (-0.4, 0.0, 0.4):
            seg = _rot(np.array([[t * s, -0.5 * s], [t * s, 0.5 * s]]), c.angle, cx, cy)
            cv2.line(img, tuple(seg[0]), tuple(seg[1]), acc, 2, aa)
    elif c.kind == "bolt":
        shaft = _rot(np.array([[-0.2 * s, -s], [0.2 * s, -s], [0.2 * s, 0.6 * s], [-0.2 * s, 0.6 * s]]), c.angle, cx, cy)
        cv2.fillConvexPoly(img, shaft, col, aa)
        for t in np.linspace(-0.8, 0.4, 5):
            th = _rot(np.array([[-0.2 * s, t * s], [0.2 * s, t * s + 0.1 * s]]), c.angle, cx, cy)
            cv2.line(img, tuple(th[0]), tuple(th[1]), dark, 1, aa)
        head = _rot(np.array([[-0.5 * s, 0.6 * s], [0.5 * s, 0.6 * s], [0.5 * s, s], [-0.5 * s, s]]), c.angle, cx, cy)
        cv2.fillConvexPoly(img, head, acc, aa)
    elif c.kind == "resistor":
        body = _rot(np.array([[-s, -0.35 * s], [s, -0.35 * s], [s, 0.35 * s], [-s, 0.35 * s]]), c.angle, cx, cy)
        cv2.fillConvexPoly(img, body, col, aa)
        for k, t in enumerate((-0.6, -0.25, 0.1, 0.55)):
            band = _rot(np.array([[t * s, -0.35 * s], [(t + 0.15) * s, -0.35 * s], [(t + 0.15) * s, 0.35 * s], [t * s, 0.35 * s]]), c.angle, cx, cy)
            cv2.fillConvexPoly(img, band, acc if k % 2 == 0 else dark, aa)
    elif c.kind == "foreign":
        pts = _rot(np.array([[s * math.cos(t) * (0.6 + 0.4 * ((k * 7) % 5) / 4), s * math.sin(t) * (0.6 + 0.4 * ((k * 3) % 5) / 4)] for k, t in enumerate(np.linspace(0, 2 * math.pi, 9)[:-1])]), c.angle, cx, cy)
        cv2.fillPoly(img, [pts], col, aa)
        cv2.circle(img, (round(cx), round(cy)), max(1, round(s * 0.25)), acc, -1, aa)
    else:
        raise ValueError(c.kind)


def render(scene: Scene) -> np.ndarray:
    """Render the scene on a canvas of (H + 2*MARGIN, W + 2*MARGIN)."""
    rng = np.random.default_rng(scene.seed + 7)
    ch, cw = H + 2 * MARGIN, W + 2 * MARGIN
    tex = cv2.resize(rng.normal(0, 1, (ch // 8, cw // 8)).astype(np.float32), (cw, ch), interpolation=cv2.INTER_CUBIC)
    img = np.clip(np.array(scene.bg_color, np.float32) + 10 * tex[..., None], 0, 255)
    x1, y1, x2, y2 = (v + MARGIN for v in TRAY)
    tray_tex = rng.normal(0, 3, (y2 - y1, x2 - x1, 1)).astype(np.float32)
    img[y1:y2, x1:x2] = np.clip(np.array(scene.tray_color, np.float32) + tray_tex, 0, 255)
    img = img.astype(np.uint8)
    cv2.rectangle(img, (x1, y1), (x2, y2), tuple(int(v * 0.6) for v in scene.tray_color), 3, cv2.LINE_AA)
    for sx1, sy1, sx2, sy2 in slot_rects():
        cv2.rectangle(img, (sx1 + MARGIN, sy1 + MARGIN), (sx2 + MARGIN, sy2 + MARGIN), scene.slot_color, -1, cv2.LINE_AA)
        cv2.rectangle(img, (sx1 + MARGIN, sy1 + MARGIN), (sx2 + MARGIN, sy2 + MARGIN), tuple(int(v * 0.7) for v in scene.slot_color), 2, cv2.LINE_AA)
    if scene.markers:
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        q = QUIET_ZONE
        for mid, (mx, my) in enumerate(MARKER_SPOTS):
            mx, my = mx + MARGIN, my + MARGIN
            cv2.rectangle(img, (mx - q, my - q), (mx + MARKER_SIZE + q, my + MARKER_SIZE + q), (255, 255, 255), -1)
            img[my : my + MARKER_SIZE, mx : mx + MARKER_SIZE] = cv2.cvtColor(cv2.aruco.generateImageMarker(dictionary, mid, MARKER_SIZE), cv2.COLOR_GRAY2BGR)
    for c in scene.components + scene.foreign:
        draw_component(img, c, MARGIN)
    return img


def component_bbox(c: Component) -> list[int]:
    m = np.zeros((H + 2 * MARGIN, W + 2 * MARGIN, 1), np.uint8)
    draw_component(m, c, MARGIN, mask=True)
    x, y, w, h = cv2.boundingRect(m)
    return [x - MARGIN, y - MARGIN, w, h]


def _overlaps(box, others, margin=6) -> bool:
    x, y, w, h = box
    return any(not (x + w + margin <= ox or ox + ow + margin <= x or y + h + margin <= oy or oy + oh + margin <= y) for ox, oy, ow, oh in others)


def _inside_tray(box, pad=8) -> bool:
    x, y, w, h = box
    return x >= TRAY[0] + 70 and y >= TRAY[1] + pad and x + w <= TRAY[2] - 70 and y + h <= TRAY[3] - pad


def marker_zone_boxes() -> list[list[int]]:
    """Marker + white quiet zone boxes [x, y, w, h] in reference px."""
    return [[mx - QUIET_ZONE, my - QUIET_ZONE, MARKER_SIZE + 2 * QUIET_ZONE, MARKER_SIZE + 2 * QUIET_ZONE] for mx, my in MARKER_SPOTS]


def box_gap(a, b) -> float:
    """Euclidean gap between two axis-aligned boxes (0 if they touch or overlap)."""
    dx = max(0, b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2]))
    dy = max(0, b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3]))
    return float(math.hypot(dx, dy))


def _foreign_near_marker(rng, scene: Scene) -> Component | None:
    """Foreign object 2-25 px outside a marker's quiet zone, on free tray surface."""
    boxes = [component_bbox(c) for c in scene.components]
    zones = marker_zone_boxes()
    for _ in range(2000):
        zx, zy, zw, zh = zones[int(rng.integers(4))]
        size = rng.uniform(13, 20)
        c = Component("foreign", rng.uniform(zx - 40, zx + zw + 40), rng.uniform(zy - 40, zy + zh + 40), size, rng.uniform(0, 180),
                      _far_color(rng, scene.tray_color, 110), _color(rng, 0, 255))
        box = component_bbox(c)
        gaps = [box_gap(box, z) for z in zones]
        inside = box[0] >= TRAY[0] + 6 and box[1] >= TRAY[1] + 6 and box[0] + box[2] <= TRAY[2] - 6 and box[1] + box[3] <= TRAY[3] - 6
        if inside and 2 <= min(gaps) <= 25 and not _overlaps(box, boxes):
            return c
    return None


# --------------------------------------------------------------------------- #
# Perturbations
# --------------------------------------------------------------------------- #
def random_homography(rng, jitter: float, shift: float, base_shift=(0.0, 0.0)) -> np.ndarray:
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    t = rng.uniform(-shift, shift, 2) + base_shift
    dst = (src + rng.uniform(-jitter, jitter, (4, 2)) + t).astype(np.float32)
    return cv2.getPerspectiveTransform(src, dst).astype(np.float64)


def photometric(img: np.ndarray, rng, strength: float, gamma_scale: float = 0.15) -> tuple[np.ndarray, dict]:
    if strength <= 0:
        return img, {}
    gain = 1 + rng.uniform(-0.25, 0.25) * strength
    offset = rng.uniform(-20, 20) * strength
    cast = 1 + rng.uniform(-0.10, 0.10, 3) * strength
    gamma = 1 + rng.uniform(-gamma_scale, gamma_scale) * strength
    g = rng.uniform(0, 0.15) * strength
    theta = rng.uniform(0, 2 * math.pi)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    ramp = ((xx - W / 2) * math.cos(theta) + (yy - H / 2) * math.sin(theta)) / (max(W, H) / 2)
    illum = (1 + g * ramp)[..., None]
    f = img.astype(np.float32) / 255.0
    f = np.power(f, gamma) * 255.0
    out = np.clip((f * gain + offset) * cast * illum, 0, 255).astype(np.uint8)
    return out, {"gain": gain, "offset": offset, "cast": cast.tolist(), "gamma": gamma, "gradient": g}


def degrade(img: np.ndarray, rng, blur: float, noise: float, jpeg: int | None) -> np.ndarray:
    out = img
    if blur > 0:
        out = cv2.GaussianBlur(out, (0, 0), blur)
    if noise > 0:
        out = np.clip(out.astype(np.float32) + rng.normal(0, noise, out.shape), 0, 255).astype(np.uint8)
    if jpeg:
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg)])
        out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return out


def to_inspection(canvas: np.ndarray, H_true: np.ndarray) -> np.ndarray:
    T = np.array([[1, 0, -MARGIN], [0, 1, -MARGIN], [0, 0, 1]], np.float64)
    return cv2.warpPerspective(canvas, H_true @ T, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


# --------------------------------------------------------------------------- #
# Case generation
# --------------------------------------------------------------------------- #
CASE_PARAMS = {
    #                    jitter shift photo  blur  noise jpeg
    "identical":        (0,     0,    0.0,   0.0,  0.0,  None),
    "lighting":         (0,     0,    1.0,   0.0,  1.5,  None),
    "geometric":        (28,    25,   0.0,   0.0,  1.5,  None),
    "blur_noise":       (4,     4,    0.0,   1.6,  6.0,  70),
    "combined_nominal": (14,    12,   0.6,   0.8,  3.0,  85),
    "missing":          (8,     8,    0.3,   0.5,  2.0,  90),
    "shifted":          (8,     8,    0.3,   0.5,  2.0,  90),
    "foreign_object":   (8,     8,    0.3,   0.5,  2.0,  90),
    "missing_hard":     (26,    22,   1.0,   1.0,  4.0,  80),
    "subtle_foreign":   (8,     8,    0.3,   0.5,  2.0,  90),
    "unrelated_scene":  (8,     8,    0.3,   0.5,  2.0,  90),
    "low_overlap":      (8,     8,    0.3,   0.5,  2.0,  90),
    "heavy_blur":       (8,     8,    0.3,   7.0,  4.0,  80),
    # D2.1 (markers always present; stronger gamma via GAMMA_SCALE)
    "gamma_lighting":      (6,  6,    1.0,   0.0,  1.5,  None),
    "missing_near_marker": (8,  8,    0.6,   0.5,  2.0,  90),
    "foreign_near_marker": (8,  8,    0.6,   0.5,  2.0,  90),
    "different_tray":      (8,  8,    0.3,   0.5,  2.0,  90),
}
GAMMA_SCALE = {"gamma_lighting": 0.30, "missing_near_marker": 0.30, "foreign_near_marker": 0.30}  # default 0.15


def _foreign(rng, scene: Scene, subtle: bool) -> Component | None:
    boxes = [component_bbox(c) for c in scene.components]
    for _ in range(200):
        size = rng.uniform(7, 10) if subtle else rng.uniform(13, 22)
        if subtle:
            color = tuple(int(np.clip(v + rng.choice([-1, 1]) * rng.uniform(25, 40), 0, 255)) for v in scene.tray_color)
            accent = color
        else:
            color, accent = _far_color(rng, scene.tray_color, 110), _color(rng, 0, 255)
        c = Component("foreign", rng.uniform(TRAY[0] + 80, TRAY[2] - 80), rng.uniform(TRAY[1] + 25, TRAY[3] - 25), size, rng.uniform(0, 180), color, accent)
        box = component_bbox(c)
        if _inside_tray(box) and not _overlaps(box, boxes):
            return c
    return None


def make_case(case_type: str, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    if case_type in MARKER_FORCED_TYPES:
        markers = True
    else:  # unchanged from D2 so older case types draw identical random numbers
        markers = case_type not in ("unrelated_scene", "heavy_blur") and bool(rng.random() < 0.5)
    scene = make_scene(int(rng.integers(1 << 30)), markers)
    insp_scene = replace(scene, components=list(scene.components), foreign=[])
    gt_boxes: list[list[int]] = []
    defect_type = None

    if case_type in ("missing", "missing_hard"):
        k = int(rng.integers(len(scene.components)))
        gt_boxes.append(component_bbox(scene.components[k]))
        insp_scene.components.pop(k)
        defect_type = "missing"
    elif case_type == "shifted":
        others_all = [component_bbox(c) for c in scene.components]
        for _ in range(400):
            k = int(rng.integers(len(scene.components)))
            ang, mag = rng.uniform(0, 2 * math.pi), rng.uniform(35, 55)
            moved = replace(scene.components[k], cx=scene.components[k].cx + mag * math.cos(ang), cy=scene.components[k].cy + mag * math.sin(ang))
            nb = component_bbox(moved)
            if _inside_tray(nb) and not _overlaps(nb, others_all[:k] + others_all[k + 1 :]):
                gt_boxes = [others_all[k], nb]
                insp_scene.components[k] = moved
                break
        else:
            raise RuntimeError(f"could not place shifted component for seed {seed}")
        defect_type = "shifted"
    elif case_type in ("foreign_object", "subtle_foreign"):
        f = _foreign(rng, scene, subtle=case_type == "subtle_foreign")
        if f is None:
            raise RuntimeError(f"could not place foreign object for seed {seed}")
        insp_scene.foreign = [f]
        gt_boxes.append(component_bbox(f))
        defect_type = "foreign_object"
    elif case_type == "unrelated_scene":
        insp_scene = make_scene(int(rng.integers(1 << 30)), markers=False)
    elif case_type == "missing_near_marker":
        zones = marker_zone_boxes()
        gaps = [min(box_gap(component_bbox(c), z) for z in zones) for c in scene.components]
        nearest = np.argsort(gaps)[:4]  # components in the slots closest to the markers
        k = int(nearest[int(rng.integers(len(nearest)))])
        gt_boxes.append(component_bbox(scene.components[k]))
        insp_scene.components.pop(k)
        defect_type = "missing"
    elif case_type == "foreign_near_marker":
        f = _foreign_near_marker(rng, scene)
        if f is None:
            raise RuntimeError(f"could not place near-marker foreign object for seed {seed}")
        insp_scene.foreign = [f]
        gt_boxes.append(component_bbox(f))
        defect_type = "foreign_object"
    elif case_type == "different_tray":
        insp_scene = make_scene(int(rng.integers(1 << 30)), markers=True)

    jitter, shift, photo, blur, noise, jpeg = CASE_PARAMS[case_type]
    base = (0.5 * W * (1 if rng.random() < 0.5 else -1), 0.0) if case_type == "low_overlap" else (0.0, 0.0)
    H_true = np.eye(3) if case_type == "identical" else random_homography(rng, jitter, shift, base)

    ref_canvas = render(scene)
    reference = ref_canvas[MARGIN : MARGIN + H, MARGIN : MARGIN + W].copy()
    inspection = reference.copy() if case_type == "identical" else to_inspection(render(insp_scene), H_true)
    inspection, photo_params = photometric(inspection, rng, photo, GAMMA_SCALE.get(case_type, 0.15))
    inspection = degrade(inspection, rng, blur, noise, jpeg)

    if case_type in NOMINAL_TYPES:
        category, expected_status = "nominal", "analyzed"
    elif case_type in DEFECT_TYPES:
        category, expected_status = "defect", "analyzed"
    elif case_type in DIFFERENT_TRAY_TYPES:
        # Registers geometrically (same fixture markers) but the contents differ; any
        # outcome other than "analyzed with no candidates" is safe.
        category, expected_status = "different_tray", "not_approved"
    else:
        category, expected_status = "registration_fail", "registration_failed"
    zones = marker_zone_boxes() if markers else []
    return {
        "case_type": case_type,
        "category": category,
        "expected_status": expected_status,
        "expected_defect": category == "defect",
        "defect_type": defect_type,
        "ambiguous": case_type == "subtle_foreign",
        "gt_boxes_xywh": gt_boxes,
        "gt_marker_gap_px": [round(min(box_gap(b, z) for z in zones), 2) if zones else None for b in gt_boxes],
        "H_true": H_true.tolist() if case_type != "unrelated_scene" else None,
        "markers": markers,
        "marker_zone_boxes_xywh": zones,
        "perturbation": {"jitter": jitter, "shift": shift, "photometric": photo_params, "blur": blur, "noise": noise, "jpeg": jpeg},
        "_images": (reference, inspection),
    }


def generate(out: Path, seed: int, per_type: int, types: list[str] | None = None) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    cases = []
    for case_type in types or CASE_TYPES:
        for i in range(per_type):
            case_seed = int(np.random.SeedSequence([seed, CASE_TYPES.index(case_type), i]).generate_state(1)[0])
            case = make_case(case_type, case_seed)
            case_id = f"{case_type}_{i:02d}"
            d = out / case_id
            d.mkdir(exist_ok=True)
            ref, ins = case.pop("_images")
            cv2.imwrite(str(d / "reference.png"), ref)
            cv2.imwrite(str(d / "inspection.png"), ins)
            cases.append({"id": case_id, "seed": case_seed, **case, "reference": f"{case_id}/reference.png", "inspection": f"{case_id}/inspection.png"})
    manifest = {"generator": "loopcheck.synth", "seed": seed, "per_type": per_type, "image_size": [W, H], "cases": cases}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return cases


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Generate synthetic reference/inspection pairs with ground truth.")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--per-type", type=int, default=8)
    p.add_argument("--case-set", choices=("d2", "d21"), default="d21",
                   help="d2: the 13 original case types (reproduces the D2 splits); d21: all types incl. marker cases")
    p.add_argument("--types", nargs="*", choices=CASE_TYPES, help="explicit subset (overrides --case-set)")
    args = p.parse_args(argv)
    types = args.types or (D2_CASE_TYPES if args.case_set == "d2" else CASE_TYPES)
    cases = generate(args.out, args.seed, args.per_type, types)
    print(f"wrote {len(cases)} cases to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
