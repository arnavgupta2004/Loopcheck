"""End-to-end deterministic inspection: validate -> register -> align -> compare -> report."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import OPENCV_VERSION, __version__
from .compare import compare
from .config import PipelineConfig
from .imageio import InputError, validate_array
from .registration import overlap_mask, project_points, register, scale_homography, warp_to_reference

STATUS_ANALYZED = "analyzed"
STATUS_REGISTRATION_FAILED = "registration_failed"
STATUS_INPUT_ERROR = "input_error"


@dataclass
class InspectionResult:
    report: dict
    images: dict[str, np.ndarray] = field(default_factory=dict)


def _resize_for_work(img: np.ndarray, max_dim: int) -> tuple[np.ndarray, float]:
    s = min(1.0, max_dim / max(img.shape[:2]))
    if s == 1.0:
        return img, 1.0
    return cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA), s


def inspect(reference: np.ndarray, inspection: np.ndarray, config: PipelineConfig | None = None, input_info: dict | None = None) -> InspectionResult:
    """Run the deterministic pipeline on two in-memory images (any of 1/3/4 channels)."""
    cfg = config or PipelineConfig()
    t0 = time.perf_counter()
    report: dict = {
        "loopcheck_version": __version__,
        "opencv_version": OPENCV_VERSION,
        "status": None,
        "coordinate_convention": "H maps reference px -> inspection px; boxes are in reference px",
        "inputs": input_info or {},
        "config": cfg.to_dict(),
    }
    try:
        ref, ref_info = validate_array(reference)
        ins, ins_info = validate_array(inspection)
    except InputError as exc:
        report.update(status=STATUS_INPUT_ERROR, error={"code": exc.code, "message": exc.message})
        return InspectionResult(report)
    report["inputs"].setdefault("reference", {}).update(ref_info)
    report["inputs"].setdefault("inspection", {}).update(ins_info)

    ref_w, s_r = _resize_for_work(ref, cfg.work_max_dim)
    ins_w, s_i = _resize_for_work(ins, cfg.work_max_dim)
    report["working_resolution"] = {"reference_scale": s_r, "inspection_scale": s_i, "reference_shape": list(ref_w.shape[:2])}

    t_reg = time.perf_counter()
    reg = register(ref_w, ins_w, cfg.registration)
    report["registration"] = {
        "ok": reg.ok,
        "method": reg.method,
        "failure_reason": reg.failure_reason,
        "metrics": reg.metrics,
        "attempts": reg.attempts,
        "seconds": round(time.perf_counter() - t_reg, 4),
    }
    if not reg.ok:
        report["status"] = STATUS_REGISTRATION_FAILED
        report["candidate_regions"] = []
        report["seconds_total"] = round(time.perf_counter() - t0, 4)
        return InspectionResult(report, {"overlay": _failure_overlay(ins, reg.failure_reason)})

    H_full = scale_homography(reg.H, 1.0 / s_r, 1.0 / s_i)
    report["registration"]["homography_ref_to_insp"] = (H_full / H_full[2, 2]).round(8).tolist()

    aligned = warp_to_reference(ins_w, reg.H, ref_w.shape)
    valid = overlap_mask(ins_w.shape, reg.H, ref_w.shape, cfg.registration.border_erode_px)

    t_cmp = time.perf_counter()
    cmp = compare(ref_w, aligned, valid, cfg.compare)
    report["comparison"] = {**cmp.diagnostics, "seconds": round(time.perf_counter() - t_cmp, 4)}

    regions = []
    for idx, r in enumerate(cmp.regions):
        r = dict(r)
        x, y, w, h = r.pop("bbox_xywh")
        corners = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float64)
        regions.append(
            {
                "id": idx,
                "bbox_xywh_reference": [round(v / s_r, 2) for v in (x, y, w, h)],
                "polygon_inspection": (project_points(corners, reg.H) / s_i).round(2).tolist(),
                "bbox_xywh_working": [x, y, w, h],
                **r,
            }
        )
    report["status"] = STATUS_ANALYZED
    report["outcome"] = "candidates_found" if regions else "no_candidates"
    report["candidate_regions"] = regions
    report["seconds_total"] = round(time.perf_counter() - t0, 4)

    images = {
        "aligned": aligned,
        "heatmap": _heatmap(aligned, cmp.anomaly_map, valid),
        "overlay": _overlay(ins, regions),
        "anomaly_map": (cmp.anomaly_map * 255).round().astype(np.uint8),
    }
    return InspectionResult(report, images)


# --------------------------------------------------------------------------- #
# Visualisation
# --------------------------------------------------------------------------- #
def _overlay(ins: np.ndarray, regions: list[dict]) -> np.ndarray:
    out = ins.copy()
    thick = max(2, round(max(ins.shape[:2]) / 400))
    for r in regions:
        poly = np.array(r["polygon_inspection"], np.float32).round().astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(out, [poly], True, (0, 0, 255), thick, cv2.LINE_AA)
        x, y = poly.reshape(-1, 2).min(axis=0)
        label = f"#{r['id']} {r['mean_score']:.2f} {r['dominant_cue']}"
        _label(out, label, (int(x), int(y) - 4), (0, 0, 255))
    banner = f"{len(regions)} candidate region(s)" if regions else "no candidate regions"
    _label(out, banner, (8, 22), (0, 160, 0) if not regions else (0, 0, 255))
    return out


def _failure_overlay(ins: np.ndarray, reason: str | None) -> np.ndarray:
    out = ins.copy()
    _label(out, f"REGISTRATION FAILED: {reason}", (8, 22), (0, 140, 255))
    return out


def _heatmap(aligned: np.ndarray, anomaly: np.ndarray, valid: np.ndarray) -> np.ndarray:
    heat = cv2.applyColorMap((anomaly * 255).round().astype(np.uint8), cv2.COLORMAP_JET)
    out = cv2.addWeighted(aligned, 0.5, heat, 0.5, 0)
    out[valid == 0] = (out[valid == 0] * 0.3).astype(np.uint8)
    return out


def _label(img: np.ndarray, text: str, org: tuple[int, int], color) -> None:
    scale = max(0.5, max(img.shape[:2]) / 1400)
    x, y = max(0, org[0]), max(int(16 * scale) + 2, org[1])
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    cv2.rectangle(img, (x, y - th - base), (x + tw + 4, y + base), (255, 255, 255), -1)
    cv2.putText(img, text, (x + 2, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)
