#!/usr/bin/env python3
"""LoopCheck D1: verify the OpenCV 5 environment with executable smoke tests.

Every check runs a real operation on synthetic images and validates the
numeric result against a known ground truth. Merely finding an attribute is
never counted as a pass.

Usage:
    python scripts/check_opencv5.py [--json results/opencv5_check.json]

Exit code is 0 only if the OpenCV major version is 5 and every check passes.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

try:
    import cv2
except ImportError as exc:  # pragma: no cover - environment failure path
    print(f"FATAL: cannot import cv2: {exc}")
    sys.exit(2)

RNG_SEED = 1234


# --------------------------------------------------------------------------- #
# Result bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    name: str
    passed: bool
    seconds: float
    details: dict = field(default_factory=dict)
    error: str | None = None


CHECKS: list[tuple[str, Callable[[], dict]]] = []


def check(name: str):
    def register(fn: Callable[[], dict]):
        CHECKS.append((name, fn))
        return fn

    return register


class CheckFailed(AssertionError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


# --------------------------------------------------------------------------- #
# Synthetic data helpers
# --------------------------------------------------------------------------- #
def textured_scene(h: int = 480, w: int = 640, seed: int = RNG_SEED) -> np.ndarray:
    """Grayscale scene with random shapes: rich, non-repetitive texture."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w), 40, np.uint8)
    for _ in range(60):
        color = int(rng.integers(60, 255))
        kind = rng.integers(0, 3)
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        if kind == 0:
            cv2.circle(img, (x, y), int(rng.integers(5, 40)), color, -1)
        elif kind == 1:
            x2, y2 = x + int(rng.integers(10, 80)), y + int(rng.integers(10, 80))
            cv2.rectangle(img, (x, y), (x2, y2), color, -1)
        else:
            pts = rng.integers(0, [w, h], size=(3, 2)).astype(np.int32)
            cv2.fillPoly(img, [pts], color)
    noise = rng.normal(0, 4, img.shape)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def known_homography() -> np.ndarray:
    """A moderate perspective warp: rotation, scale, shift and slight tilt."""
    return np.array(
        [[0.97, -0.08, 18.0], [0.07, 0.99, -12.0], [4e-5, -3e-5, 1.0]],
        dtype=np.float64,
    )


def corner_error(h_est: np.ndarray, h_true: np.ndarray, w: int, h: int) -> float:
    """Mean reprojection distance (px) of the four image corners."""
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    p_est = cv2.perspectiveTransform(corners, h_est)
    p_true = cv2.perspectiveTransform(corners, h_true)
    return float(np.linalg.norm(p_est - p_true, axis=2).mean())


def sift_matches(img_a: np.ndarray, img_b: np.ndarray, ratio: float = 0.75):
    sift = cv2.SIFT_create(nfeatures=2000)
    kp_a, des_a = sift.detectAndCompute(img_a, None)
    kp_b, des_b = sift.detectAndCompute(img_b, None)
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    knn = matcher.knnMatch(des_a, des_b, k=2)
    good = [m for m, n in (p for p in knn if len(p) == 2) if m.distance < ratio * n.distance]
    src = np.float32([kp_a[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp_b[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    return kp_a, kp_b, des_a, good, src, dst


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
@check("version_is_opencv5")
def _version() -> dict:
    major = int(cv2.__version__.split(".")[0])
    require(cv2.__version__.startswith("5."), f"cv2.__version__ = {cv2.__version__!r}")
    return {"cv2.__version__": cv2.__version__, "major": major, "module_path": cv2.__file__}


@check("aruco_detection_and_homography")
def _aruco() -> dict:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    canvas_h, canvas_w, size, margin = 600, 800, 120, 40
    canvas = np.full((canvas_h, canvas_w), 255, np.uint8)
    # Four markers at the tray corners, ids 0..3.
    origins = {
        0: (margin, margin),
        1: (canvas_w - margin - size, margin),
        2: (canvas_w - margin - size, canvas_h - margin - size),
        3: (margin, canvas_h - margin - size),
    }
    for marker_id, (x, y) in origins.items():
        canvas[y : y + size, x : x + size] = cv2.aruco.generateImageMarker(dictionary, marker_id, size)

    h_true = known_homography()
    warped = cv2.warpPerspective(canvas, h_true, (canvas_w, canvas_h), borderValue=255)

    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(dictionary, params)
    corners, ids, rejected = detector.detectMarkers(warped)
    require(ids is not None, "no markers detected")
    found = sorted(int(i) for i in ids.ravel())
    require(found == [0, 1, 2, 3], f"expected ids [0,1,2,3], got {found}")

    # Reference corners are known exactly; detected corners are in the warped frame.
    src, dst = [], []
    for c, marker_id in zip(corners, ids.ravel()):
        x, y = origins[int(marker_id)]
        src.extend([[x, y], [x + size, y], [x + size, y + size], [x, y + size]])
        dst.extend(c.reshape(4, 2).tolist())
    src = np.float32(src).reshape(-1, 1, 2)
    dst = np.float32(dst).reshape(-1, 1, 2)
    h_est, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    require(h_est is not None, "findHomography returned None")
    err = corner_error(h_est, h_true, canvas_w, canvas_h)
    # Marker corners sit at pixel-edge positions; allow ~1 px of convention offset.
    require(err < 2.0, f"ArUco homography corner error {err:.3f}px >= 2.0px")
    return {
        "detected_ids": found,
        "rejected_candidates": len(rejected),
        "corners_shape": list(corners[0].shape),
        "homography_corner_error_px": round(err, 4),
        "inliers": int(mask.sum()),
    }


@check("sift_detect_and_match")
def _sift() -> dict:
    ref = textured_scene()
    h_true = known_homography()
    insp = cv2.warpPerspective(ref, h_true, (ref.shape[1], ref.shape[0]))
    kp_a, kp_b, des_a, good, src, dst = sift_matches(ref, insp)
    require(len(kp_a) > 200 and len(kp_b) > 200, f"too few keypoints: {len(kp_a)}, {len(kp_b)}")
    require(des_a.shape[1] == 128 and des_a.dtype == np.float32, f"unexpected descriptor {des_a.shape} {des_a.dtype}")
    require(len(good) > 100, f"only {len(good)} ratio-test matches")
    # Ground-truth check: fraction of matches consistent with the known warp.
    proj = cv2.perspectiveTransform(src, h_true)
    correct = float((np.linalg.norm(proj - dst, axis=2) < 3.0).mean())
    require(correct > 0.8, f"only {correct:.2%} of matches are geometrically correct")
    return {
        "keypoints_ref": len(kp_a),
        "keypoints_insp": len(kp_b),
        "descriptor_shape": list(des_a.shape),
        "ratio_test_matches": len(good),
        "fraction_correct_matches": round(correct, 4),
    }


@check("usac_magsac_homography")
def _usac() -> dict:
    ref = textured_scene()
    h_true = known_homography()
    insp = cv2.warpPerspective(ref, h_true, (ref.shape[1], ref.shape[0]))
    _, _, _, good, src, dst = sift_matches(ref, insp)

    # Inject 40% gross outliers so robustness is actually exercised.
    rng = np.random.default_rng(RNG_SEED)
    n_out = int(len(src) * 0.4)
    bad = rng.uniform([0, 0], [ref.shape[1], ref.shape[0]], size=(n_out, 1, 2)).astype(np.float32)
    src_all = np.concatenate([src, src[:n_out]])
    dst_all = np.concatenate([dst, bad])

    results = {}
    for label, method in (("USAC_MAGSAC", cv2.USAC_MAGSAC), ("USAC_DEFAULT", cv2.USAC_DEFAULT), ("RANSAC", cv2.RANSAC)):
        h_est, mask = cv2.findHomography(src_all, dst_all, method, 3.0, maxIters=5000, confidence=0.999)
        require(h_est is not None, f"{label}: findHomography returned None")
        err = corner_error(h_est, h_true, ref.shape[1], ref.shape[0])
        results[label] = {"corner_error_px": round(err, 4), "inliers": int(mask.sum()), "total": len(src_all)}
        require(err < 1.0, f"{label}: corner error {err:.3f}px >= 1.0px")

    # UsacParams object API.
    p = cv2.UsacParams()
    p.threshold = 3.0
    p.confidence = 0.999
    p.maxIterations = 5000
    p.sampler = cv2.SAMPLING_UNIFORM
    p.score = cv2.SCORE_METHOD_MAGSAC
    h_p, mask_p = cv2.findHomography(src_all, dst_all, mask=None, params=p)
    require(h_p is not None, "findHomography(params=UsacParams) returned None")
    err_p = corner_error(h_p, h_true, ref.shape[1], ref.shape[0])
    require(err_p < 1.0, f"UsacParams: corner error {err_p:.3f}px")
    results["UsacParams_MAGSAC_score"] = {"corner_error_px": round(err_p, 4), "inliers": int(mask_p.sum())}
    results["injected_outliers"] = n_out
    return results


@check("ecc_alignment")
def _ecc() -> dict:
    ref = cv2.GaussianBlur(textured_scene(), (0, 0), 1.5).astype(np.float32) / 255.0
    h, w = ref.shape
    # Small homography perturbation: ECC is a local refiner, not a global matcher.
    h_true = np.array([[1.01, 0.012, 3.5], [-0.01, 0.995, -2.5], [1e-5, 0.0, 1.0]], np.float64)
    insp = cv2.warpPerspective(ref, h_true, (w, h))

    out = {}
    # Homography motion model.
    warp = np.eye(3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    cc, warp = cv2.findTransformECC(ref, insp, warp, cv2.MOTION_HOMOGRAPHY, criteria, None, 5)
    # findTransformECC estimates W with template(x) ~ input(W x), i.e. W = h_true here.
    err = corner_error(warp.astype(np.float64), h_true, w, h)
    require(cc > 0.95, f"ECC homography correlation {cc:.4f} <= 0.95")
    require(err < 0.5, f"ECC homography corner error {err:.3f}px >= 0.5px")
    out["homography"] = {"ecc_cc": round(float(cc), 5), "corner_error_px": round(err, 4)}

    # Affine motion model on a pure affine shift.
    a_true = np.array([[1.0, 0.0, 4.0], [0.0, 1.0, -3.0]], np.float32)
    insp_a = cv2.warpAffine(ref, a_true, (w, h))
    warp_a = np.eye(2, 3, dtype=np.float32)
    cc_a, warp_a = cv2.findTransformECC(ref, insp_a, warp_a, cv2.MOTION_AFFINE, criteria, None, 5)
    shift_err = float(np.abs(warp_a[:, 2] - a_true[:, 2]).max())
    require(shift_err < 0.2, f"ECC affine shift error {shift_err:.3f}px")
    out["affine"] = {"ecc_cc": round(float(cc_a), 5), "max_shift_error_px": round(shift_err, 4)}

    # computeECC as a similarity score after alignment.
    aligned = cv2.warpPerspective(insp, warp, (w, h), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
    score = cv2.computeECC(ref, aligned)
    out["computeECC_after_alignment"] = round(float(score), 5)
    return out


@check("template_matching")
def _template() -> dict:
    scene = textured_scene()
    y0, x0, th, tw = 200, 300, 60, 80
    templ = scene[y0 : y0 + th, x0 : x0 + tw].copy()
    out = {}
    for label, method in (("TM_CCOEFF_NORMED", cv2.TM_CCOEFF_NORMED), ("TM_SQDIFF_NORMED", cv2.TM_SQDIFF_NORMED)):
        res = cv2.matchTemplate(scene, templ, method)
        require(res.shape == (scene.shape[0] - th + 1, scene.shape[1] - tw + 1), f"{label}: result shape {res.shape}")
        min_v, max_v, min_loc, max_loc = cv2.minMaxLoc(res)
        loc = min_loc if method == cv2.TM_SQDIFF_NORMED else max_loc
        require(loc == (x0, y0), f"{label}: found {loc}, expected {(x0, y0)}")
        out[label] = {"location": list(loc), "score": round(float(max_v if loc == max_loc else min_v), 5)}

    # Displaced copy under brightness change: the "misplaced component" scenario.
    moved = scene.copy()
    moved[y0 : y0 + th, x0 : x0 + tw] = 40
    nx, ny = x0 + 37, y0 - 25
    moved[ny : ny + th, nx : nx + tw] = np.clip(templ.astype(np.int16) + 25, 0, 255).astype(np.uint8)
    res = cv2.matchTemplate(moved, templ, cv2.TM_CCOEFF_NORMED)
    _, max_v, _, max_loc = cv2.minMaxLoc(res)
    require(max_loc == (nx, ny), f"displaced: found {max_loc}, expected {(nx, ny)}")
    require(max_v > 0.95, f"displaced: score {max_v:.4f}")
    out["displaced_with_brightness_offset"] = {"location": list(max_loc), "score": round(float(max_v), 5)}

    # Masked template matching (signature with mask argument).
    mask = np.full(templ.shape, 255, np.uint8)
    res_m = cv2.matchTemplate(scene, templ, cv2.TM_CCORR_NORMED, mask=mask)
    _, _, _, loc_m = cv2.minMaxLoc(res_m)
    require(loc_m == (x0, y0), f"masked TM_CCORR_NORMED found {loc_m}")
    out["masked_TM_CCORR_NORMED"] = list(loc_m)
    return out


@check("filtering_gradients_threshold_morphology")
def _filters() -> dict:
    img = np.zeros((200, 200), np.uint8)
    cv2.rectangle(img, (50, 50), (149, 149), 200, -1)
    out = {}

    blur = cv2.GaussianBlur(img, (0, 0), sigmaX=2.0)
    require(blur.shape == img.shape and blur.dtype == np.uint8, "GaussianBlur shape/dtype")
    require(blur[100, 100] == 200 and blur[100, 50] < 200 and blur[100, 49] > 0, "GaussianBlur edge profile")
    out["gaussian_center_edge_outside"] = [int(blur[100, 100]), int(blur[100, 50]), int(blur[100, 49])]

    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    require(gx[100, 49] > 0 and gx[100, 150] < 0, "Sobel x sign at left/right edges")
    require(abs(float(gx[100, 100])) < 1e-6 and abs(float(gy[100, 100])) < 1e-6, "Sobel non-zero in flat region")
    out["sobel_gx_left_right"] = [float(gx[100, 49]), float(gx[100, 150])]
    out["sobel_mag_max"] = float(mag.max())

    noisy = np.clip(blur.astype(np.int16) + np.random.default_rng(RNG_SEED).integers(-10, 10, img.shape), 0, 255).astype(np.uint8)
    t_otsu, bw = cv2.threshold(noisy, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    require(30 < t_otsu < 180, f"Otsu threshold {t_otsu}")
    fg = int((bw > 0).sum())
    require(abs(fg - 100 * 100) < 600, f"Otsu foreground area {fg}")
    out["otsu_threshold"] = float(t_otsu)
    out["otsu_foreground_px"] = fg

    adapt = cv2.adaptiveThreshold(noisy, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, -5)
    require(adapt.shape == img.shape and set(np.unique(adapt)) <= {0, 255}, "adaptiveThreshold output")
    out["adaptive_unique_values"] = [int(v) for v in np.unique(adapt)]

    # Morphology: opening removes isolated specks, closing fills a hole.
    specks = bw.copy()
    specks[10, 10] = specks[20, 180] = 255
    specks[100, 100] = 0
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    opened = cv2.morphologyEx(specks, cv2.MORPH_OPEN, kernel)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel)
    require(opened[10, 10] == 0 and opened[20, 180] == 0, "MORPH_OPEN did not remove specks")
    require(closed[100, 100] == 255, "MORPH_CLOSE did not fill hole")
    out["morph_open_close"] = "specks removed, hole filled"

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    eq = clahe.apply(noisy)
    require(eq.shape == img.shape and eq.dtype == np.uint8, "CLAHE output")
    out["clahe_std_before_after"] = [round(float(noisy.std()), 2), round(float(eq.std()), 2)]

    lab = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2LAB)
    require(lab.shape == (200, 200, 3), "BGR2LAB shape")
    return out


@check("contours_and_connected_components")
def _components() -> dict:
    img = np.zeros((300, 400), np.uint8)
    blobs = [((20, 30), (80, 90)), ((150, 40), (230, 140)), ((300, 200), (380, 280))]
    for (x1, y1), (x2, y2) in blobs:
        cv2.rectangle(img, (x1, y1), (x2, y2), 255, -1)
    cv2.circle(img, (100, 220), 30, 255, -1)

    ret = cv2.findContours(img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    require(len(ret) == 2, f"findContours returned {len(ret)} values (expected 2: contours, hierarchy)")
    contours, hierarchy = ret
    require(len(contours) == 4, f"expected 4 contours, got {len(contours)}")
    boxes = sorted(cv2.boundingRect(c) for c in contours)
    areas = sorted(round(cv2.contourArea(c)) for c in contours)

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(img, connectivity=8)
    require(n == 5, f"expected 5 labels (incl. background), got {n}")
    require(stats.shape == (5, 5) and centroids.shape == (5, 2), f"stats {stats.shape}, centroids {centroids.shape}")
    rect_areas = sorted((x2 - x1 + 1) * (y2 - y1 + 1) for (x1, y1), (x2, y2) in blobs)
    cc_areas = sorted(int(a) for a in stats[1:, cv2.CC_STAT_AREA])
    for a in rect_areas:
        require(a in cc_areas, f"rectangle area {a} not in component areas {cc_areas}")
    return {
        "contour_count": len(contours),
        "hierarchy_shape": list(hierarchy.shape),
        "bounding_rects": [list(b) for b in boxes],
        "contour_areas": areas,
        "cc_labels_incl_background": int(n),
        "cc_areas": cc_areas,
        "labels_dtype": str(labels.dtype),
    }


@check("warp_and_end_to_end_diff")
def _diff() -> dict:
    """Mini integration: register with SIFT+MAGSAC, warp back, diff, find the defect."""
    ref = textured_scene()
    insp = ref.copy()
    cv2.rectangle(insp, (400, 300), (459, 359), 40, -1)  # "missing component"
    h_true = known_homography()
    h, w = ref.shape
    insp_w = cv2.warpPerspective(insp, h_true, (w, h))

    _, _, _, _, src, dst = sift_matches(ref, insp_w)
    h_est, _ = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, 3.0)
    aligned = cv2.warpPerspective(insp_w, h_est, (w, h), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
    valid = cv2.warpPerspective(np.full((h, w), 255, np.uint8), h_est, (w, h), flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP)
    valid = cv2.erode(valid, np.ones((5, 5), np.uint8))

    diff = cv2.absdiff(cv2.GaussianBlur(ref, (5, 5), 0), cv2.GaussianBlur(aligned, (5, 5), 0))
    diff = cv2.bitwise_and(diff, diff, mask=valid)
    _, bw = cv2.threshold(diff, 40, 255, cv2.THRESH_BINARY)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw)
    big = [s for s in stats[1:] if s[cv2.CC_STAT_AREA] > 200]
    require(len(big) >= 1, "defect region not found")
    x, y, bw_, bh = (int(v) for v in max(big, key=lambda s: s[cv2.CC_STAT_AREA])[:4])
    inter = max(0, min(x + bw_, 460) - max(x, 400)) * max(0, min(y + bh, 360) - max(y, 300))
    iou = inter / (bw_ * bh + 60 * 60 - inter)
    require(iou > 0.5, f"defect box IoU {iou:.3f} <= 0.5 (box {x, y, bw_, bh})")
    return {"regions_over_200px": len(big), "largest_box_xywh": [x, y, bw_, bh], "iou_vs_ground_truth": round(iou, 4)}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
BUILD_KEYS = (
    "General configuration for OpenCV",
    "Version control",
    "Platform:",
    "Host:",
    "CPU/HW features:",
    "Baseline:",
    "Dispatched code generation:",
    "Extra modules:",
    "To be built:",
    "Parallel framework:",
    "Python 3:",
    "Interpreter:",
    "Lapack:",
    "Eigen:",
    "OpenCL:",
)


def relevant_build_info() -> list[str]:
    lines = cv2.getBuildInformation().splitlines()
    return [ln.rstrip() for ln in lines if any(k in ln for k in BUILD_KEYS)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", type=Path, help="write machine-readable results to this path")
    args = parser.parse_args()

    print(f"cv2.__version__      : {cv2.__version__}")
    print(f"cv2 module           : {cv2.__file__}")
    print(f"numpy                : {np.__version__}")
    print(f"python               : {platform.python_version()} ({platform.machine()}, {platform.system()})")
    print(f"cv2.getNumThreads()  : {cv2.getNumThreads()}")
    print("--- relevant cv2.getBuildInformation() lines ---")
    build = relevant_build_info()
    for ln in build:
        print(ln)
    print("------------------------------------------------")

    results: list[CheckResult] = []
    for name, fn in CHECKS:
        t0 = time.perf_counter()
        try:
            details = fn()
            results.append(CheckResult(name, True, time.perf_counter() - t0, details))
        except Exception as exc:  # record the exact failure, never mask it
            results.append(
                CheckResult(name, False, time.perf_counter() - t0, error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            )
        r = results[-1]
        print(f"[{'PASS' if r.passed else 'FAIL'}] {name} ({r.seconds * 1000:.0f} ms)")
        if r.passed:
            for k, v in r.details.items():
                print(f"       {k}: {v}")
        else:
            print("       " + r.error.replace("\n", "\n       "))

    n_pass = sum(r.passed for r in results)
    print(f"\nSUMMARY: {n_pass}/{len(results)} checks passed on OpenCV {cv2.__version__}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cv2_version": cv2.__version__,
            "numpy_version": np.__version__,
            "python_version": platform.python_version(),
            "machine": platform.machine(),
            "build_info_excerpt": build,
            "checks": [asdict(r) for r in results],
            "passed": n_pass,
            "total": len(results),
        }
        args.json.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        print(f"wrote {args.json}")

    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
