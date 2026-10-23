"""Exclusion masks for detected ArUco fiducials, in reference coordinates.

Only markers actually detected by cv2.aruco are masked. Reference detections are drawn
directly; inspection detections are rasterised in inspection coordinates and brought
into the reference frame with the same warp used for the image comparison
(registration.warp_to_reference, i.e. WARP_INVERSE_MAP with H: reference -> inspection).

ArUco corners follow OpenCV's pixel-centre convention (the outer edge of a marker whose
first pixel column is x lies at x - 0.5); polygons are rasterised with sub-pixel
precision, and the safety margin is far larger than that half-pixel.
"""

from __future__ import annotations

import cv2
import numpy as np

from .config import MarkerMaskConfig
from .registration import project_points, warp_to_reference

_SHIFT = 4  # fillConvexPoly fractional bits (1/16 px)


def grow_quad(corners: np.ndarray, grow: float) -> np.ndarray:
    """Offset every edge of a convex quad outward by `grow` px; exact under perspective."""
    p = np.asarray(corners, np.float64).reshape(4, 2)
    c = p.mean(axis=0)
    lines = []
    for i in range(4):
        a, b = p[i], p[(i + 1) % 4]
        d = b - a
        n = np.array([d[1], -d[0]]) / max(np.linalg.norm(d), 1e-9)
        if np.dot(n, (a + b) / 2 - c) < 0:
            n = -n
        lines.append((a + n * grow, d))
    out = []
    for i in range(4):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        A = np.array([d1, -d2]).T
        t = np.linalg.solve(A, p2 - p1)[0]
        out.append(p1 + t * d1)
    return np.array(out)


def quad_side(corners: np.ndarray) -> float:
    p = np.asarray(corners, np.float64).reshape(4, 2)
    return float(np.mean([np.linalg.norm(p[(i + 1) % 4] - p[i]) for i in range(4)]))


def _fill(mask: np.ndarray, poly: np.ndarray) -> None:
    pts = np.round(poly * (1 << _SHIFT)).astype(np.int32).reshape(-1, 1, 2)
    cv2.fillConvexPoly(mask, pts, 255, cv2.LINE_8, _SHIFT)


def marker_exclusion(
    ref_shape: tuple[int, ...],
    insp_shape: tuple[int, ...],
    H: np.ndarray,
    ref_markers: dict[int, np.ndarray],
    insp_markers: dict[int, np.ndarray],
    valid: np.ndarray,
    cfg: MarkerMaskConfig,
    leak_px: int = 0,
) -> tuple[np.ndarray, dict]:
    """Return (uint8 exclusion mask in reference coords, 255 = excluded, info).

    `leak_px` is the dilation compare() will add; the total-area cap is checked on the
    dilated (effective) area so the cap bounds what is actually removed from scoring.
    """
    h, w = ref_shape[:2]
    img_area = float(h * w)
    H_inv = np.linalg.inv(H)
    ref_mask = np.zeros((h, w), np.uint8)
    ins_mask = np.zeros(insp_shape[:2], np.uint8)
    entries = []

    for source, markers in (("reference", ref_markers), ("inspection", insp_markers)):
        for mid, corners in sorted(markers.items()):
            side = quad_side(corners)
            poly = grow_quad(corners, cfg.margin_frac * side + cfg.margin_px)
            poly_ref = poly if source == "reference" else project_points(poly, H_inv)
            area = float(cv2.contourArea(poly_ref.astype(np.float32)))
            entry = {"id": int(mid), "source": source, "side_px": round(side, 2), "polygon_reference": poly_ref, "area_px": round(area, 1)}
            if not cv2.isContourConvex(np.round(poly_ref).astype(np.int32).reshape(-1, 1, 2)):
                entry.update(masked=False, reason="non_convex_polygon")
            elif area > cfg.max_marker_area_frac * img_area:
                entry.update(masked=False, reason="polygon_exceeds_max_marker_area")
            else:
                entry["masked"] = True
                _fill(ref_mask if source == "reference" else ins_mask, poly)
            entries.append(entry)

    warped = warp_to_reference(ins_mask, H, ref_shape, cv2.INTER_NEAREST)
    excl = cv2.bitwise_and(cv2.bitwise_or(ref_mask, warped), valid)
    n_valid = max(int(np.count_nonzero(valid)), 1)
    masked_px = int(np.count_nonzero(excl))

    common = sorted(set(ref_markers) & set(insp_markers))
    cross = [float(np.linalg.norm(project_points(insp_markers[i], H_inv) - ref_markers[i], axis=1).max()) for i in common]
    info = {
        "enabled": True,
        "applied": True,
        "markers": entries,
        "markers_masked": sum(e["masked"] for e in entries),
        "masked_area_px": masked_px,
        "masked_fraction_of_valid": round(masked_px / n_valid, 5),
        "common_marker_max_corner_disagreement_px": round(max(cross), 3) if cross else None,
    }
    effective = excl
    if leak_px > 0 and masked_px:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * leak_px + 1, 2 * leak_px + 1))
        effective = cv2.bitwise_and(cv2.dilate(excl, k), valid)
    effective_px = int(np.count_nonzero(effective))
    info["cap_check_area_px"] = effective_px
    if effective_px > cfg.max_total_frac * n_valid:
        info.update(applied=False, skipped_reason="total_masked_area_exceeds_cap", masked_area_px=0, masked_fraction_of_valid=0.0)
        excl = np.zeros_like(excl)
    return excl, info
