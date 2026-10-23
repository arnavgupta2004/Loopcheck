"""Bounded perception-decision-action controller for LoopCheck (D3).

The agent wraps the deterministic OpenCV 5 pipeline in an explicit loop:

    observe (state) -> policy picks ONE next tool from the observations -> execute
    -> record the observation and how it changes state -> repeat
    -> terminate with APPROVE / DEFECT / HUMAN_REVIEW and a full trace

Design rules (all enforced in code, all tested):
  * The policy is an ordered list of explicit rules over the state; every chosen action
    records the rule that fired and the observations it used.
  * Bounded: at most `max_steps` tool calls; exceeding the budget -> HUMAN_REVIEW.
  * No duplicate actions: an action key (tool + parameters) runs at most once.
  * Fail safe: any tool exception, input error or policy fault -> HUMAN_REVIEW. There is no
    path from an error to APPROVE.
  * Corroboration, not score magnitude: a candidate is CONFIRMED only when independent
    re-inspection views reproduce it; it is DISMISSED only when no view reproduces it AND
    there is a concrete explanation (illumination, local misalignment, or a weak
    sensitive-scan detection that does not reproduce). Otherwise it stays AMBIGUOUS.
  * Masked (marker) pixels are NOT INSPECTED. They are reported as such and screened with
    a separate contrast diagnostic; they are never counted as clean.
  * Scores are uncalibrated heuristics; thresholds below are decision rules tuned on the
    development split, not probabilities.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import asdict, dataclass, field, fields, replace

import cv2
import numpy as np

from . import OPENCV_VERSION, __version__
from .compare import compare, ssim_map
from .config import CompareConfig, PipelineConfig, RegistrationConfig
from .imageio import InputError, validate_array
from .pipeline import _resize_for_work, align_and_mask
from .registration import (
    RegistrationResult,
    project_points,
    register,
    sanity_check,
    scale_homography,
    to_gray,
)

APPROVE, DEFECT, HUMAN_REVIEW = "APPROVE", "DEFECT", "HUMAN_REVIEW"
CONFIRMED, DISMISSED, AMBIGUOUS, UNRESOLVED = "confirmed", "dismissed", "ambiguous", "unresolved"
PHOTOMETRIC_CUES = ("intensity", "color")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class AgentConfig:
    max_steps: int = 16
    max_reinspect: int = 6  # candidates re-inspected individually; the rest stay unresolved

    # A. registration alternatives, in order (each a genuinely different preprocessing)
    registration_alternatives: tuple[str, ...] = ("sharpness_matched_sift", "relaxed_ratio_sift")

    # B. global photometric shift: max deviation of blurred conditional L medians from a
    #    line (L units), or a large linear gain. Dev split: geometric <= 0.7, blur/noise
    #    <= 2.9, gamma cases 1.2-7.2 (overlapping). 3.0 / 0.12 activate B on 25 dev cases;
    #    6.0 / 0.20 activated it on 3 with identical decisions; 2.5 / 0.10 added 1 escalation.
    nonlinearity_trigger: float = 3.0
    gain_trigger: float = 0.12  # |L_gain - 1|
    robust_compare: dict = field(default_factory=lambda: {"global_mode": "histogram",
                                                          "weights": {"intensity": 0.1, "ssim": 0.35, "gradient": 0.35, "color": 0.2}})

    # C. candidate triage. A candidate is "strong" (confirmed without re-inspection) only
    #    when no illumination shift is suspected and both rules hold.
    strong_peak: float = 0.75
    strong_min_area_px: int = 400
    # Re-inspection corroboration: the structure-weighted crop view must reproduce the
    # candidate at >= confirm_structural_peak and >= 1 other view must reproduce it.
    # Dev split: real candidates' structural peaks were mostly >= 0.72, false ones <= 0.63.
    confirm_structural_peak: float = 0.70
    tiny_area_px: int = 300  # tiny candidates are re-inspected upsampled
    crop_pad_min_px: int = 32
    crop_min_side_px: int = 96
    persist_iou: float = 0.10
    local_align_max_shift_px: float = 3.0
    explained_shift_px: float = 0.5  # local ECC shift that counts as a misalignment explanation

    # D. sub-threshold hotspots trigger a high-sensitivity scan
    hotspot_low: float = 0.20
    hotspot_min_peak: float = 0.30
    hotspot_min_area_px: int = 20
    max_hotspots_for_scan: int = 3  # more isolated hotspots indicates a global effect, not a tiny defect
    sensitive_compare: dict = field(default_factory=lambda: {"threshold": 0.22, "min_region_peak": 0.33, "min_region_area_frac": 0.0001})

    # E. coverage / marker blind spots
    min_coverage: float = 0.80  # inspected (scored) fraction of the reference frame
    # Targeted UNMASKED diagnostic of the excluded band: crop each marker neighbourhood and
    # compare without the marker mask (crop-local tone fit). Dev split: real objects in a
    # band peaked 0.71-0.97; false findings (gamma, hard perspective) <= 0.67.
    band_crop_pad_px: int = 24
    band_min_peak: float = 0.60
    band_attach_px: float = 12.0  # band finding this close to a confirmed defect is attached to it

    # F. widespread / content mismatch
    widespread_regions: int = 6  # triggers the content check
    widespread_fraction: float = 0.02  # fraction of scored pixels above threshold; triggers the content check
    severe_widespread_fraction: float = 0.30  # still this widespread after robust comparison -> HUMAN_REVIEW
    content_min_matches: int = 20
    content_min_agreement: float = 0.20  # dev: same tray 0.32-1.00, different tray 0.03-0.13
    content_max_disp_px: float = 4.0

    # G / quality
    max_auto_blur_sigma: float = 2.0  # dev: blur_noise needed <= 1.53, heavy blur >= 2.25

    @classmethod
    def from_dict(cls, data: dict) -> "AgentConfig":
        known = {f.name for f in fields(cls)}
        bad = set(data) - known
        if bad:
            raise ValueError(f"unknown agent config keys: {sorted(bad)}")
        cfg = cls()
        for k, v in data.items():
            setattr(cfg, k, tuple(v) if k == "registration_alternatives" else v)
        return cfg


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    id: int
    bbox: list[float]  # working reference px [x, y, w, h]
    source: str  # "default" | "robust" | "sensitive"
    first_pass: dict
    status: str = UNRESOLVED
    views: list[dict] = field(default_factory=list)
    resolution_reason: str | None = None


@dataclass
class AgentState:
    ref: np.ndarray
    ins: np.ndarray
    s_ref: float
    s_ins: float
    registration_attempts: list[dict] = field(default_factory=list)
    reg: RegistrationResult | None = None
    aligned: np.ndarray | None = None
    valid: np.ndarray | None = None
    exclude: np.ndarray | None = None
    marker_info: dict | None = None
    comparisons: dict = field(default_factory=dict)  # variant -> ComparisonResult
    observations: dict = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    executed: set = field(default_factory=set)
    errors: list[dict] = field(default_factory=list)
    branches: set = field(default_factory=set)
    steps: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)  # policy decisions that need no tool call

    def summary(self) -> dict:
        by = {}
        for c in self.candidates:
            by[c.status] = by.get(c.status, 0) + 1
        return {
            "registration": None if self.reg is None else ("ok:" + str(self.reg.method) if self.reg.ok else "failed"),
            "comparisons": sorted(self.comparisons),
            "candidates": by,
            "observations": sorted(self.observations),
            "errors": len(self.errors),
        }


@dataclass
class Action:
    tool: str
    params: dict
    rule: str  # policy rule that selected it
    rationale: str
    uses: dict  # observations the rule used (name -> value)

    @property
    def key(self) -> str:
        return self.tool + ":" + ",".join(f"{k}={self.params[k]}" for k in sorted(self.params))


@dataclass
class Termination:
    decision: str
    rule: str
    reasons: list[str]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _contains(box, pt) -> bool:
    return box[0] <= pt[0] <= box[0] + box[2] and box[1] <= pt[1] <= box[1] + box[3]


def _round(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_round(x) for x in v]
    if isinstance(v, np.generic):
        return _round(v.item())
    return v


def linear_tone_residual(ref: np.ndarray, aligned: np.ndarray, mask: np.ndarray, blur_sigma: float = 3.0,
                         bin_width: int = 16, min_bin_frac: float = 0.02) -> dict:
    """Measure how non-linear the pixel-wise tone relation between the images is.

    Both images are blurred (so focus, noise and resampling differences mostly cancel);
    the conditional median of inspection L is taken per reference-L bin; a line is
    fitted to those medians (weighted by bin size). Reported: gain/offset of that line
    and the largest bin deviation from it (L units). A gamma change bends the relation;
    blur, noise and small defects barely move bin medians (each bin must hold at least
    `min_bin_frac` of the pixels, so one missing part cannot dominate a bin).
    """
    m = mask > 0
    min_bin_px = max(300, int(min_bin_frac * np.count_nonzero(m)))
    if np.count_nonzero(m) < 1000:
        return {"gain": None, "offset": None, "nonlinearity": None, "bins": 0}
    lr = cv2.GaussianBlur(cv2.cvtColor(ref, cv2.COLOR_BGR2LAB)[..., 0].astype(np.float32), (0, 0), blur_sigma)[m]
    li = cv2.GaussianBlur(cv2.cvtColor(aligned, cv2.COLOR_BGR2LAB)[..., 0].astype(np.float32), (0, 0), blur_sigma)[m]
    xs, ys, ws = [], [], []
    for lo in range(0, 256, bin_width):
        sel = (lr >= lo) & (lr < lo + bin_width)
        n = int(np.count_nonzero(sel))
        if n >= min_bin_px:
            xs.append(float(np.median(lr[sel])))
            ys.append(float(np.median(li[sel])))
            ws.append(np.sqrt(n))
    if len(xs) < 3:
        return {"gain": None, "offset": None, "nonlinearity": None, "bins": len(xs)}
    a, b = np.polyfit(xs, ys, 1, w=ws)  # L_ins ~ a * L_ref + b
    resid = np.array(ys) - (a * np.array(xs) + b)
    return {"gain": float(1.0 / a) if a else None, "offset": float(-b / a) if a else None,
            "nonlinearity": float(np.abs(resid).max()), "bins": len(xs)}


def _components(binary: np.ndarray, score: np.ndarray, min_area: int) -> list[dict]:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary.astype(np.uint8), connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in stats[i])
        if a < min_area:
            continue
        out.append({"bbox": [x, y, w, h], "area_px": a, "peak": float(score[labels == i].max())})
    return out


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class InspectionAgent:
    def __init__(self, pipeline_config: PipelineConfig | None = None, agent_config: AgentConfig | None = None):
        self.pcfg = pipeline_config or PipelineConfig()
        self.cfg = agent_config or AgentConfig()

    # ------------------------------------------------------------------ tools
    def tool_register(self, st: AgentState, variant: str) -> dict:
        """Register with the default chain or a genuinely different preprocessing."""
        rcfg = self.pcfg.registration
        if variant == "default":
            reg = register(st.ref, st.ins, rcfg)
        elif variant == "sharpness_matched_sift":
            # Blur the sharper image until focus measures match, then SIFT at half
            # resolution: recovers pairs where one image is defocused.
            r, i = to_gray(st.ref).astype(np.float32), to_gray(st.ins).astype(np.float32)
            fr, fi = (float(cv2.Laplacian(x, cv2.CV_32F).var()) for x in (r, i))
            sharp_is_ref = fr > fi
            target = min(fr, fi)
            lo, hi = 0.0, 8.0
            src = r if sharp_is_ref else i
            for _ in range(12):
                mid = (lo + hi) / 2
                if float(cv2.Laplacian(cv2.GaussianBlur(src, (0, 0), max(mid, 1e-3)), cv2.CV_32F).var()) > target:
                    lo = mid
                else:
                    hi = mid
            sigma = hi
            r2 = cv2.GaussianBlur(st.ref, (0, 0), sigma) if sharp_is_ref and sigma > 0.05 else st.ref
            i2 = st.ins if sharp_is_ref or sigma <= 0.05 else cv2.GaussianBlur(st.ins, (0, 0), sigma)
            s = 0.5
            r2 = cv2.resize(r2, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            i2 = cv2.resize(i2, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            small = replace(rcfg, min_inliers=max(12, rcfg.min_inliers // 2))
            reg = register(r2, i2, small)
            reg.metrics = {**reg.metrics, "preprocess": {"equalize_sigma": round(sigma, 3), "blurred": "reference" if sharp_is_ref else "inspection", "scale": s}}
            if reg.ok:
                H = scale_homography(reg.H, 1.0 / s, 1.0 / s)
                reason, sanity = sanity_check(H, st.ref.shape, st.ins.shape, rcfg)
                reg.H, reg.metrics = (H / H[2, 2], {**reg.metrics, **sanity})
                if reason:
                    reg.ok, reg.H, reg.failure_reason = False, None, f"full_res_{reason}"
        elif variant == "relaxed_ratio_sift":
            reg = register(st.ref, st.ins, replace(rcfg, sift_ratio=0.85, sift_nfeatures=8000, aruco_min_markers=99))
        else:
            raise ValueError(f"unknown registration variant {variant}")
        # Marker detections always come from full-resolution images of the default chain.
        if st.reg is not None and variant != "default":
            reg.markers_reference, reg.markers_inspection = st.reg.markers_reference, st.reg.markers_inspection
        attempt = {"variant": variant, "ok": reg.ok, "method": reg.method, "failure_reason": reg.failure_reason,
                   "metrics": _round({k: v for k, v in reg.metrics.items() if k != "ecc"})}
        st.registration_attempts.append(attempt)
        if reg.ok or st.reg is None:
            st.reg = reg
        if reg.ok:
            st.aligned, st.valid, st.exclude, st.marker_info = align_and_mask(st.ref, st.ins, reg, self.pcfg)
            st.comparisons.clear()
            st.candidates.clear()
        return attempt

    def _compare_cfg(self, variant: str) -> CompareConfig:
        base = self.pcfg.compare
        if variant == "default":
            return base
        if variant == "illumination_robust":
            return replace(base, **self.cfg.robust_compare)
        if variant == "sensitive":
            return replace(base, **self.cfg.sensitive_compare)
        raise ValueError(f"unknown compare variant {variant}")

    def tool_compare(self, st: AgentState, variant: str) -> dict:
        res = compare(st.ref, st.aligned, st.valid, self._compare_cfg(variant), exclude=st.exclude)
        st.comparisons[variant] = res
        out = {"variant": variant, "n_regions": len(res.regions),
               "fraction_above_threshold": res.diagnostics["fraction_above_threshold"],
               "sharpness_sigma": res.diagnostics["sharpness"]["sigma"]}
        if variant == "default":
            tone = linear_tone_residual(st.ref, st.aligned, res.scoring_mask)
            low = (res.anomaly_map >= self.cfg.hotspot_low) & (res.scoring_mask > 0)
            hot = [h for h in _components(low, res.anomaly_map, self.cfg.hotspot_min_area_px)
                   if h["peak"] >= self.cfg.hotspot_min_peak
                   and not any(_iou(h["bbox"], r["bbox_xywh"]) > 0 for r in res.regions)]
            ref_area = st.ref.shape[0] * st.ref.shape[1]
            coverage = {
                "reference_px": ref_area,
                "observed_fraction": round(np.count_nonzero(st.valid) / ref_area, 4),
                "inspected_fraction": round(np.count_nonzero(res.scoring_mask) / ref_area, 4),
                "excluded_markers_fraction": round(res.diagnostics["excluded_px"] / ref_area, 4),
            }
            st.observations.update(photometric=_round(tone), hotspots=len(hot), hotspot_peaks=_round([h["peak"] for h in hot][:10]),
                                   hotspot_boxes=[h["bbox"] for h in hot][:20],
                                   coverage=coverage, sharpness=res.diagnostics["sharpness"],
                                   widespread={"n_regions": len(res.regions), "fraction_above_threshold": res.diagnostics["fraction_above_threshold"]})
            out.update(photometric=_round(tone), hotspots=len(hot), coverage=coverage)
            self._add_candidates(st, res.regions, "default")
        elif variant == "illumination_robust":
            added = self._annotate_and_add(st, res.regions, "robust")
            out["new_candidates"] = added
            st.observations["robust_regions"] = len(res.regions)
        elif variant == "sensitive":
            # Targeted: only detections at the observed hotspots become candidates.
            hot = st.observations.get("hotspot_boxes", [])
            at_hot = [r for r in res.regions if any(_iou(r["bbox_xywh"], h) > 0 for h in hot)]
            added = self._annotate_and_add(st, at_hot, "sensitive")
            out.update(new_candidates=added, regions_not_at_hotspots_ignored=len(res.regions) - len(at_hot))
        out["candidates_after"] = len(st.candidates)
        return out

    def _add_candidates(self, st: AgentState, regions: list[dict], source: str) -> int:
        n = 0
        for r in regions:
            st.candidates.append(Candidate(len(st.candidates), [float(v) for v in r["bbox_xywh"]], source,
                                           {k: r[k] for k in ("peak_score", "mean_score", "area_px", "dominant_cue", "change_hint", "evidence")}))
            n += 1
        return n

    def _annotate_and_add(self, st: AgentState, regions: list[dict], source: str) -> int:
        new = [r for r in regions if not any(_iou(r["bbox_xywh"], c.bbox) > 0 for c in st.candidates)]
        for c in st.candidates:
            c.first_pass[f"seen_in_{source}"] = any(_iou(r["bbox_xywh"], c.bbox) > 0 for r in regions)
        return self._add_candidates(st, new, source)

    def _crop_view(self, st: AgentState, cand: Candidate, name: str, scale: float, ccfg: CompareConfig, local_align: bool) -> dict:
        H, W = st.ref.shape[:2]
        x, y, w, h = cand.bbox
        pad = max(self.cfg.crop_pad_min_px, int(max(w, h)))
        x0, y0 = int(max(0, x - pad)), int(max(0, y - pad))
        x1, y1 = int(min(W, x + w + pad)), int(min(H, y + h + pad))
        while (x1 - x0) < self.cfg.crop_min_side_px and (x0 > 0 or x1 < W):
            x0, x1 = max(0, x0 - 8), min(W, x1 + 8)
        while (y1 - y0) < self.cfg.crop_min_side_px and (y0 > 0 or y1 < H):
            y0, y1 = max(0, y0 - 8), min(H, y1 + 8)
        rc = st.ref[y0:y1, x0:x1]
        ac = st.aligned[y0:y1, x0:x1]
        vc = st.valid[y0:y1, x0:x1]
        ec = None if st.exclude is None else st.exclude[y0:y1, x0:x1]
        view = {"view": name, "crop_xyxy": [x0, y0, x1, y1], "scale": scale}
        if np.count_nonzero(vc) < 0.3 * vc.size:
            return {**view, "error": "crop mostly outside observed overlap"}
        if local_align:
            g = lambda im: cv2.GaussianBlur(to_gray(im).astype(np.float32) / 255.0, (0, 0), 1.0)  # noqa: E731
            warp = np.eye(2, 3, dtype=np.float32)
            try:
                _, warp = cv2.findTransformECC(g(rc), g(ac), warp, cv2.MOTION_TRANSLATION,
                                               (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5), None, 3)
            except cv2.error as exc:
                return {**view, "error": f"local ECC failed: {str(exc).strip().splitlines()[-1][:120]}"}
            shift = float(np.hypot(warp[0, 2], warp[1, 2]))
            view["local_shift_px"] = round(shift, 3)
            if shift > self.cfg.local_align_max_shift_px:
                return {**view, "error": f"local shift {shift:.2f}px exceeds {self.cfg.local_align_max_shift_px}px"}
            ac = cv2.warpAffine(ac, warp, (ac.shape[1], ac.shape[0]), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
        if scale != 1.0:
            interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
            rc, ac = (cv2.resize(im, None, fx=scale, fy=scale, interpolation=interp) for im in (rc, ac))
            vc = cv2.resize(vc, (rc.shape[1], rc.shape[0]), interpolation=cv2.INTER_NEAREST)
            ec = None if ec is None else cv2.resize(ec, (rc.shape[1], rc.shape[0]), interpolation=cv2.INTER_NEAREST)
        res = compare(rc, ac, vc, ccfg, exclude=ec)
        box_c = [(x - x0) * scale, (y - y0) * scale, w * scale, h * scale]
        centre = (box_c[0] + box_c[2] / 2, box_c[1] + box_c[3] / 2)
        hits = [r for r in res.regions if _iou(r["bbox_xywh"], box_c) >= self.cfg.persist_iou or _contains(r["bbox_xywh"], centre)]
        view.update(persist=bool(hits), peak=round(max((r["peak_score"] for r in hits), default=0.0), 4),
                    mean=round(max((r["mean_score"] for r in hits), default=0.0), 4), regions_in_crop=len(res.regions))
        return view

    def tool_reinspect(self, st: AgentState, candidate: int) -> dict:
        """Crop the candidate and re-run the comparison under several views (multi-scale,
        crop-local normalisation, local re-alignment, structural weights)."""
        cand = st.candidates[candidate]
        base = replace(self.pcfg.compare, normalize_local=False)  # global fit inside the crop = local fit
        if cand.source == "sensitive":  # re-check weak detections at the sensitivity that produced them
            base = replace(base, **self.cfg.sensitive_compare)
        tiny = cand.first_pass["area_px"] < self.cfg.tiny_area_px
        views = [("crop_local_1x", 1.0, base, False),
                 ("crop_local_2x" if tiny else "crop_local_0.5x", 2.0 if tiny else 0.5, base, False),
                 ("crop_local_aligned_1x", 1.0, base, True),
                 ("crop_structural_1x", 1.0, replace(base, **self.cfg.robust_compare), False)]
        for name, scale, ccfg, la in views:
            try:
                cand.views.append(self._crop_view(st, cand, name, scale, ccfg, la))
            except Exception as exc:  # a failing view is recorded, never counted as "clean"
                cand.views.append({"view": name, "error": f"{type(exc).__name__}: {exc}"})
        self._adjudicate(st, cand)
        return {"candidate": cand.id, "views": cand.views, "status": cand.status, "reason": cand.resolution_reason}

    def tool_marker_band_check(self, st: AgentState) -> dict:
        """Targeted UNMASKED diagnostic of the excluded marker band. Each band component
        (exclusion + leak dilation) is cropped with context and compared WITHOUT the marker
        mask, using a crop-local tone fit, so the marker's own black/white extremes are fitted
        locally instead of by the whole-image fit that caused the D2 marker false positives.
        Regions touching the band at peak >= band_min_peak are findings."""
        band = st.exclude.copy()
        if self.pcfg.compare.exclude_mode == "dilate" and self.pcfg.compare.exclude_leak_px > 0:
            r = self.pcfg.compare.exclude_leak_px
            band = cv2.dilate(band, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
        band = cv2.bitwise_and(band, st.valid)
        ccfg = replace(self.pcfg.compare, normalize_local=False)
        H, W = st.ref.shape[:2]
        p = self.cfg.band_crop_pad_px
        n, _, stats, _ = cv2.connectedComponentsWithStats((band > 0).astype(np.uint8))
        finds = []
        for i in range(1, n):
            x, y, w, h, _a = (int(v) for v in stats[i])
            x0, y0, x1, y1 = max(0, x - p), max(0, y - p), min(W, x + w + p), min(H, y + h + p)
            res = compare(st.ref[y0:y1, x0:x1], st.aligned[y0:y1, x0:x1], st.valid[y0:y1, x0:x1], ccfg)  # no exclusion
            bc = band[y0:y1, x0:x1]
            for reg in res.regions:
                rx, ry, rw, rh = reg["bbox_xywh"]
                if reg["peak_score"] >= self.cfg.band_min_peak and np.any(bc[ry:ry + rh, rx:rx + rw]):
                    finds.append({"bbox": [rx + x0, ry + y0, rw, rh], "peak": reg["peak_score"], "dominant_cue": reg["dominant_cue"]})
        confirmed = [c for c in st.candidates if c.status == CONFIRMED]
        for f in finds:
            fx, fy, fw, fh = f["bbox"]
            near = [c.id for c in confirmed if _iou([fx - self.cfg.band_attach_px, fy - self.cfg.band_attach_px,
                                                      fw + 2 * self.cfg.band_attach_px, fh + 2 * self.cfg.band_attach_px], c.bbox) > 0]
            f["attached_to_confirmed"] = near
        unexplained = [f for f in finds if not f["attached_to_confirmed"]]
        st.observations["marker_band"] = {"band_px": int(np.count_nonzero(band)), "findings": _round(finds), "unexplained": len(unexplained)}
        return st.observations["marker_band"]

    def tool_content_check(self, st: AgentState) -> dict:
        """Do reference and aligned inspection share content outside the markers? SIFT
        matches between the two images in reference coordinates should sit at ~zero
        displacement if the contents are the same item."""
        mask = cv2.bitwise_and(st.valid, cv2.bitwise_not(st.exclude)) if st.exclude is not None else st.valid
        sift = cv2.SIFT_create(nfeatures=3000)
        kr, dr = sift.detectAndCompute(to_gray(st.ref), mask)
        ka, da = sift.detectAndCompute(to_gray(st.aligned), mask)
        out = {"keypoints_reference": len(kr), "keypoints_inspection": len(ka)}
        if dr is None or da is None or len(kr) < 2 or len(ka) < 2:
            out.update(matches=0, agreement=None)
        else:
            knn = cv2.BFMatcher(cv2.NORM_L2).knnMatch(dr, da, k=2)
            good = [p[0] for p in knn if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
            disp = [np.hypot(kr[m.queryIdx].pt[0] - ka[m.trainIdx].pt[0], kr[m.queryIdx].pt[1] - ka[m.trainIdx].pt[1]) for m in good]
            agree = float(np.mean(np.array(disp) <= self.cfg.content_max_disp_px)) if disp else None
            out.update(matches=len(good), agreement=None if agree is None else round(agree, 4))
        out["mismatch"] = bool(out["matches"] < self.cfg.content_min_matches or (out["agreement"] is not None and out["agreement"] < self.cfg.content_min_agreement))
        st.observations["content"] = out
        return out

    # ------------------------------------------------------------------ adjudication
    def _illumination_suspect(self, st: AgentState) -> bool:
        p = st.observations.get("photometric") or {}
        if p.get("nonlinearity") is None or p.get("gain") is None:
            return False
        return p["nonlinearity"] >= self.cfg.nonlinearity_trigger or abs(p["gain"] - 1.0) >= self.cfg.gain_trigger

    def _adjudicate(self, st: AgentState, cand: Candidate) -> None:
        """Corroboration rules (see module docstring). The structure-weighted view is the
        discriminating one: photometric artefacts reproduce in the photometric crop views
        (they share the cause) but not with structural weights."""
        ok = {v["view"]: v for v in cand.views if "error" not in v}
        struct = next((v for k, v in ok.items() if k.startswith("crop_structural")), None)
        others = [v for k, v in ok.items() if not k.startswith("crop_structural")]
        n_other = sum(v["persist"] for v in others)
        if struct is None or len(others) < 2:
            cand.status, cand.resolution_reason = AMBIGUOUS, f"too few usable re-inspection views ({len(ok)}); structural view {'missing' if struct is None else 'ok'}"
            return
        s_peak = struct["peak"] if struct["persist"] else 0.0
        if s_peak >= self.cfg.confirm_structural_peak and n_other >= 1:
            if cand.source == "sensitive":
                cand.status, cand.resolution_reason = AMBIGUOUS, (f"high-sensitivity detection reproduced (structural {s_peak:.2f}, {n_other}/{len(others)} other views) "
                                                                  "but the high-sensitivity mode has a high false-positive rate: escalate")
            else:
                cand.status, cand.resolution_reason = CONFIRMED, f"structural view reproduces it (peak {s_peak:.2f} >= {self.cfg.confirm_structural_peak}) and {n_other}/{len(others)} other views agree"
            return
        if not struct["persist"] and n_other == 0:
            shifts = [v.get("local_shift_px", 0.0) for v in others]
            photometric = cand.first_pass["dominant_cue"] in PHOTOMETRIC_CUES
            if cand.source == "sensitive":
                cand.status, cand.resolution_reason = DISMISSED, "high-sensitivity detection not reproduced in any view at its own sensitivity"
            elif self._illumination_suspect(st) and photometric and cand.first_pass.get("seen_in_robust") is False:
                cand.status, cand.resolution_reason = DISMISSED, ("photometric-cue candidate under a measured illumination shift; absent from the full-image "
                                                                  "illumination-robust comparison and from every local view")
            elif max(shifts, default=0.0) >= self.cfg.explained_shift_px:
                cand.status, cand.resolution_reason = DISMISSED, f"not reproduced after local re-alignment ({max(shifts):.2f}px residual shift)"
            else:
                cand.status, cand.resolution_reason = AMBIGUOUS, "not reproduced in any view, but no explanation for the first-pass response"
            return
        if not struct["persist"] and n_other > 0 and cand.source != "sensitive" and self._illumination_suspect(st) \
                and cand.first_pass["dominant_cue"] in PHOTOMETRIC_CUES and cand.first_pass.get("seen_in_robust") is False:
            cand.status, cand.resolution_reason = DISMISSED, (f"photometric-only response ({n_other}/{len(others)} photometric views, structural view absent, "
                                                              "absent from the full-image illumination-robust comparison) under a measured illumination shift")
            return
        cand.status, cand.resolution_reason = AMBIGUOUS, (f"conflicting evidence: structural {'peak ' + format(s_peak, '.2f') if struct['persist'] else 'absent'}, "
                                                          f"{n_other}/{len(others)} other views reproduce it")

    # ------------------------------------------------------------------ policy
    def policy(self, st: AgentState) -> Action | Termination:
        """Ordered rules: the first rule whose condition holds selects the next action."""
        cfg, ex = self.cfg, st.executed

        def act(tool, params, rule, why, uses):
            return Action(tool, params, rule, why, uses)

        # A. registration
        if not st.registration_attempts:
            return act("register", {"variant": "default"}, "R0", "no alignment yet", {})
        if st.reg is None or not st.reg.ok:
            last = st.registration_attempts[-1]
            for v in cfg.registration_alternatives:
                a = act("register", {"variant": v}, "A1", f"registration failed ({last['failure_reason']}); try a different preprocessing", {"last_failure": last["failure_reason"]})
                if a.key not in ex:
                    st.branches.add("A")
                    return a
            st.branches.add("A")
            return Termination(HUMAN_REVIEW, "A2", [f"registration unreliable after {len(st.registration_attempts)} attempts: " + ", ".join(f"{a['variant']}={a['failure_reason']}" for a in st.registration_attempts)])

        # first-pass comparison
        if "default" not in st.comparisons:
            return act("compare", {"variant": "default"}, "R1", "aligned; run the default comparison", {"registration": st.reg.method})
        obs = st.observations

        # image quality: a heavily defocused pair cannot be decided automatically
        sigma = obs["sharpness"]["sigma"]
        if sigma >= cfg.max_auto_blur_sigma:
            return Termination(HUMAN_REVIEW, "Q1", [f"sharpness equalisation needed sigma {sigma:.2f} >= {cfg.max_auto_blur_sigma}: image too blurred for an automatic decision"])

        # F. content identity: marker alignment alone does not prove the same item; widespread differences call for it too
        widespread = obs["widespread"]["n_regions"] >= cfg.widespread_regions or obs["widespread"]["fraction_above_threshold"] >= cfg.widespread_fraction
        if (st.reg.method == "aruco" or widespread) and "content_check" not in {k.split(":")[0] for k in ex}:
            st.branches.add("F")
            why = "aligned by markers only; verify the contents match" if st.reg.method == "aruco" else "widespread differences; check content identity"
            return act("content_check", {}, "F1", why, {"method": st.reg.method, "widespread": obs["widespread"]})
        if obs.get("content", {}).get("mismatch"):
            st.branches.add("F")
            c = obs["content"]
            return Termination(HUMAN_REVIEW, "F2", [f"content mismatch: {c['matches']} content matches, agreement {c['agreement']} (min {cfg.content_min_matches} / {cfg.content_min_agreement}); reference and inspected item may differ"])

        # B. illumination shift -> alternative comparison emphasising structure
        if self._illumination_suspect(st) and "illumination_robust" not in st.comparisons:
            st.branches.add("B")
            return act("compare", {"variant": "illumination_robust"}, "B1", "strong global tone change measured; add a structure-weighted comparison", {"photometric": obs["photometric"]})

        if obs["widespread"]["fraction_above_threshold"] >= cfg.severe_widespread_fraction:
            st.branches.add("F")
            robust = st.comparisons.get("illumination_robust")
            robust_frac = None if robust is None else robust.diagnostics["fraction_above_threshold"]
            if robust_frac is None or robust_frac >= cfg.severe_widespread_fraction:
                return Termination(HUMAN_REVIEW, "F3", [f"severe widespread differences (fraction {obs['widespread']['fraction_above_threshold']}, robust {robust_frac}) "
                                                        "not explained by the content check or an illumination-robust comparison"])

        # D. a few isolated sub-threshold hotspots -> targeted high-sensitivity scan
        if 0 < obs["hotspots"] <= cfg.max_hotspots_for_scan and "sensitive" not in st.comparisons:
            st.branches.add("D")
            return act("compare", {"variant": "sensitive"}, "D1", f"{obs['hotspots']} isolated sub-threshold hotspot(s); scan them at higher sensitivity", {"hotspots": obs["hotspots"], "peaks": obs["hotspot_peaks"]})
        if obs["hotspots"] > cfg.max_hotspots_for_scan and not any(n["rule"] == "D0" for n in st.notes):
            st.notes.append({"after_step": len(st.steps) - 1, "rule": "D0", "note": f"{obs['hotspots']} sub-threshold hotspots > {cfg.max_hotspots_for_scan}: "
                             "pattern of a global effect, targeted sensitive scan skipped (tiny defects may be missed)"})

        # C. candidate triage
        illum = self._illumination_suspect(st)
        pending = [c for c in st.candidates if c.status == UNRESOLVED]
        reinspected = sum(1 for c in st.candidates if c.views)
        source_rank = {"default": 0, "robust": 1, "sensitive": 2}
        for c in sorted(pending, key=lambda c: (source_rank[c.source], -c.first_pass["peak_score"])):
            fp = c.first_pass
            if c.source == "default" and not illum and fp["peak_score"] >= cfg.strong_peak and fp["area_px"] >= cfg.strong_min_area_px:
                c.status, c.resolution_reason = CONFIRMED, f"strong first-pass evidence (peak {fp['peak_score']} >= {cfg.strong_peak}, area {fp['area_px']} >= {cfg.strong_min_area_px}) without illumination shift"
                st.notes.append({"after_step": len(st.steps) - 1, "rule": "C0", "note": f"candidate {c.id} confirmed without re-inspection: {c.resolution_reason}"})
                continue
            if reinspected >= cfg.max_reinspect:
                break
            st.branches.add("D" if c.source == "sensitive" or fp["area_px"] < cfg.tiny_area_px else "C")
            return act("reinspect", {"candidate": c.id}, "C1",
                       f"{c.source} candidate {c.id} needs corroboration (peak {fp['peak_score']}, area {fp['area_px']}, cue {fp['dominant_cue']}{', illumination shift' if illum else ''})",
                       {"first_pass": {k: fp[k] for k in ("peak_score", "area_px", "dominant_cue")}})

        # E. marker blind spots
        if st.exclude is not None and np.any(st.exclude) and "marker_band" not in obs:
            st.branches.add("E")
            return act("marker_band_check", {}, "E1", "marker pixels were excluded (not inspected); screen the band", {"excluded_fraction": obs["coverage"]["excluded_markers_fraction"]})

        return self.decide(st)

    # ------------------------------------------------------------------ decision
    def decide(self, st: AgentState) -> Termination:
        cfg, obs = self.cfg, st.observations
        confirmed = [c for c in st.candidates if c.status == CONFIRMED]
        unresolved = [c for c in st.candidates if c.status in (AMBIGUOUS, UNRESOLVED)]
        band = obs.get("marker_band", {"unexplained": 0})
        cov = obs["coverage"]
        if confirmed:
            reasons = [f"{len(confirmed)} confirmed defect region(s)"]
            if unresolved:
                reasons.append(f"{len(unresolved)} further candidate(s) unresolved (listed)")
            if band["unexplained"]:
                reasons.append(f"{band['unexplained']} unexplained finding(s) in the marker blind spot (listed)")
            return Termination(DEFECT, "T1", reasons)
        if unresolved:
            return Termination(HUMAN_REVIEW, "T2", [f"{len(unresolved)} candidate(s) neither confirmed nor explained"])
        if band["unexplained"]:
            st.branches.add("E")
            return Termination(HUMAN_REVIEW, "T3", [f"{band['unexplained']} discrepancy(ies) inside the excluded marker band; it was not inspected by the main comparison"])
        if cov["inspected_fraction"] < cfg.min_coverage:
            return Termination(HUMAN_REVIEW, "T4", [f"inspected fraction {cov['inspected_fraction']} < {cfg.min_coverage}"])
        st.branches.add("G")
        dismissed = [c for c in st.candidates if c.status == DISMISSED]
        reasons = [f"reliable registration ({st.reg.method}), inspected {cov['inspected_fraction']:.1%} of the reference frame, no unresolved evidence"]
        if dismissed:
            reasons.append(f"{len(dismissed)} candidate(s) dismissed with explanation (listed)")
        if cov["excluded_markers_fraction"] > 0:
            reasons.append(f"{cov['excluded_markers_fraction']:.1%} excluded (markers): screened by contrast diagnostic, NOT inspected")
        return Termination(APPROVE, "T5", reasons)

    # ------------------------------------------------------------------ run loop
    def run(self, reference: np.ndarray, inspection: np.ndarray) -> dict:
        """Run the controller. The final AgentState (aligned image, comparison maps) is kept
        on `self.last_state` for visualisation; it does not influence the decision."""
        self.last_state = None
        t0 = time.perf_counter()
        report = {"loopcheck_version": __version__, "opencv_version": OPENCV_VERSION, "mode": "agent",
                  "agent_config": _round(asdict(self.cfg)), "coordinate_convention": "boxes in reference px"}
        try:
            ref, _ = validate_array(reference)
            ins, _ = validate_array(inspection)
        except InputError as exc:
            report.update(decision=HUMAN_REVIEW, termination_rule="E0", reasons=[f"input error {exc.code}: {exc.message}"], steps=[], seconds=round(time.perf_counter() - t0, 4))
            return report
        ref_w, s_r = _resize_for_work(ref, self.pcfg.work_max_dim)
        ins_w, s_i = _resize_for_work(ins, self.pcfg.work_max_dim)
        st = AgentState(ref_w, ins_w, s_r, s_i)
        self.last_state = st
        tools = {"register": self.tool_register, "compare": self.tool_compare, "reinspect": self.tool_reinspect,
                 "marker_band_check": self.tool_marker_band_check, "content_check": self.tool_content_check}

        term: Termination | None = None
        while term is None:
            try:
                nxt = self.policy(st)
            except Exception as exc:
                term = Termination(HUMAN_REVIEW, "X1", [f"policy error {type(exc).__name__}: {exc}"])
                st.errors.append({"where": "policy", "error": traceback.format_exc(limit=3)})
                break
            if isinstance(nxt, Termination):
                term = nxt
                break
            if nxt.key in st.executed:
                term = Termination(HUMAN_REVIEW, "X2", [f"policy proposed a duplicate action {nxt.key}; stopping safely"])
                break
            if len(st.steps) >= self.cfg.max_steps:
                term = Termination(HUMAN_REVIEW, "X3", [f"step budget {self.cfg.max_steps} exhausted before a decision"])
                break
            st.executed.add(nxt.key)
            before = st.summary()
            ts = time.perf_counter()
            step = {"index": len(st.steps), "state_before": before, "rule": nxt.rule, "tool": nxt.tool, "params": nxt.params,
                    "rationale": nxt.rationale, "observations_used": _round(nxt.uses)}
            try:
                step["output"] = _round(tools[nxt.tool](st, **nxt.params))
            except Exception as exc:
                step["error"] = f"{type(exc).__name__}: {exc}"
                st.errors.append({"where": nxt.key, "error": traceback.format_exc(limit=3)})
                term = Termination(HUMAN_REVIEW, "X4", [f"tool {nxt.tool} failed: {type(exc).__name__}: {exc}"])
            step["seconds"] = round(time.perf_counter() - ts, 4)
            step["state_after"] = st.summary()
            step["effect"] = _effect(before, step["state_after"])
            st.steps.append(step)

        report.update(self._final_report(st, term))
        report["seconds"] = round(time.perf_counter() - t0, 4)
        return report

    def _final_report(self, st: AgentState, term: Termination) -> dict:
        def cand_out(c: Candidate) -> dict:
            x, y, w, h = c.bbox
            out = {"id": c.id, "source": c.source, "status": c.status, "reason": c.resolution_reason,
                   "bbox_xywh_reference": [round(v / st.s_ref, 2) for v in (x, y, w, h)],
                   "first_pass": _round(c.first_pass), "views": _round(c.views)}
            if st.reg is not None and st.reg.ok:
                corners = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float64)
                out["polygon_inspection"] = (project_points(corners, st.reg.H) / st.s_ins).round(2).tolist()
            return out

        limitations = []
        mi = st.marker_info
        polys = []
        if mi and mi.get("applied"):
            polys = [{"id": e["id"], "source": e["source"], "polygon_reference": (np.asarray(e["polygon_reference"]) / st.s_ref).round(2).tolist()}
                     for e in mi["markers"] if e["masked"]]
        if polys:
            limitations.append("pixels inside excluded marker polygons (+ leak dilation) were NOT inspected by the main comparison")
        if term.decision == APPROVE:
            limitations.append("defects below the comparison's sensitivity (very small / low-contrast) can be missed")
        if st.reg is not None and st.reg.ok and st.reg.method == "aruco" and "content" not in st.observations:
            limitations.append("alignment by markers only; content identity not verified")
        n_tools = len(st.steps)
        return {
            "decision": term.decision,
            "termination_rule": term.rule,
            "reasons": term.reasons,
            "branches": sorted(st.branches),
            "registration": {"attempts": st.registration_attempts, "final_method": st.reg.method if st.reg and st.reg.ok else None,
                             "homography_ref_to_insp": None if not (st.reg and st.reg.ok) else (scale_homography(st.reg.H, 1 / st.s_ref, 1 / st.s_ins)).round(8).tolist()},
            "coverage": st.observations.get("coverage"),
            "excluded_polygons_reference": polys,
            "observations": _round({k: v for k, v in st.observations.items() if k != "coverage"}),
            "defects": [cand_out(c) for c in st.candidates if c.status == CONFIRMED] if term.decision == DEFECT else [],
            "candidates": [cand_out(c) for c in st.candidates],
            "resolution_counts": {s: sum(c.status == s for c in st.candidates) for s in (CONFIRMED, DISMISSED, AMBIGUOUS, UNRESOLVED)},
            "tool_calls": n_tools,
            "errors": st.errors,
            "limitations": limitations,
            "steps": st.steps,
            "notes": st.notes,
        }


def _effect(before: dict, after: dict) -> str:
    changes = []
    for k in before:
        if before[k] == after[k]:
            continue
        if isinstance(before[k], list) and isinstance(after[k], list):
            added, removed = sorted(set(after[k]) - set(before[k])), sorted(set(before[k]) - set(after[k]))
            changes.append(f"{k}: " + " ".join([f"+{a}" for a in added] + [f"-{r}" for r in removed]))
        else:
            changes.append(f"{k}: {before[k]} -> {after[k]}")
    return "; ".join(changes) if changes else "no state change"


def format_trace(report: dict) -> str:
    """Human-readable terminal rendering of an agent report."""
    lines = [f"LoopCheck agent (OpenCV {report.get('opencv_version')})"]
    for s in report.get("steps", []):
        lines.append(f"[{s['index']}] {s['rule']} {s['tool']}({', '.join(f'{k}={v}' for k, v in s['params'].items())})  {s['seconds']:.2f}s")
        lines.append(f"     why: {s['rationale']}")
        if "error" in s:
            lines.append(f"     ERROR: {s['error']}")
        lines.append(f"     effect: {s['effect']}")
        for n in report.get("notes", []):
            if n["after_step"] == s["index"]:
                lines.append(f"     note ({n['rule']}): {n['note']}")
    for c in report.get("candidates", []):
        lines.append(f"  candidate {c['id']} ({c['source']}) {c['status']}: {c['reason']}  box={c['bbox_xywh_reference']}")
    lines.append(f"DECISION: {report['decision']}  (rule {report.get('termination_rule')})")
    for r in report.get("reasons", []):
        lines.append(f"  - {r}")
    for lim in report.get("limitations", []):
        lines.append(f"  limitation: {lim}")
    return "\n".join(lines)


def draw_agent_overlay(ins: np.ndarray, report: dict) -> np.ndarray:
    """Inspection image with candidates coloured by status and excluded (not inspected) areas."""
    out = ins.copy()
    colours = {CONFIRMED: (0, 0, 255), AMBIGUOUS: (0, 165, 255), UNRESOLVED: (0, 215, 255), DISMISSED: (160, 160, 160)}
    H = report.get("registration", {}).get("homography_ref_to_insp")
    if H is not None:
        for e in report.get("excluded_polygons_reference", []):
            poly = project_points(np.asarray(e["polygon_reference"]), np.asarray(H))
            cv2.polylines(out, [np.round(poly).astype(np.int32).reshape(-1, 1, 2)], True, (255, 0, 255), 1, cv2.LINE_AA)
    for c in report.get("candidates", []):
        if "polygon_inspection" not in c:
            continue
        pts = np.round(np.asarray(c["polygon_inspection"])).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(out, [pts], True, colours[c["status"]], 2, cv2.LINE_AA)
        x, y = pts.reshape(-1, 2).min(axis=0)
        cv2.putText(out, f"#{c['id']} {c['status']}", (int(x), max(12, int(y) - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colours[c["status"]], 1, cv2.LINE_AA)
    banner = {APPROVE: (0, 150, 0), DEFECT: (0, 0, 255), HUMAN_REVIEW: (0, 140, 255)}[report["decision"]]
    cv2.rectangle(out, (0, 0), (out.shape[1], 26), (255, 255, 255), -1)
    cv2.putText(out, f"{report['decision']}  (rule {report.get('termination_rule')}; magenta = excluded, not inspected)", (6, 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, banner, 1, cv2.LINE_AA)
    return out
