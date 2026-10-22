"""Photometric normalisation, difference cues, fusion and region extraction.

All inputs are in reference coordinates: `ref` and `aligned` are 8-bit BGR images of
the same size and `valid` is a uint8 mask (255 = both images observed this pixel).

Cues (each mapped to [0, 1] by a configurable scale, then fused by a weighted mean):
  intensity  LAB lightness difference              (missing / added material)
  ssim       1 - local SSIM on lightness             (texture / structure change)
  gradient   Sobel gradient-magnitude difference     (edges appear / disappear; robust to gain)
  color      LAB a/b chroma distance                 (colour change at similar brightness)

Intensity, gradient and colour use a symmetric misalignment-tolerant distance: a pixel
counts as different only if its value lies outside the min..max range of the other
image in a (2r+1)^2 neighbourhood. This suppresses sub-pixel registration residue on
edges without hiding solid changes larger than r pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import CompareConfig

CUES = ("intensity", "ssim", "gradient", "color")


@dataclass
class ComparisonResult:
    anomaly_map: np.ndarray  # float32 in [0, 1], zero outside the valid mask
    cues: dict[str, np.ndarray]
    regions: list[dict]
    diagnostics: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Photometric normalisation
# --------------------------------------------------------------------------- #
def _masked_blur(img: np.ndarray, weight: np.ndarray, sigma: float) -> np.ndarray:
    """Normalised convolution: Gaussian mean of img using only pixels with weight > 0."""
    num = cv2.GaussianBlur(img * weight, (0, 0), sigma)
    den = cv2.GaussianBlur(weight, (0, 0), sigma)
    return num / np.maximum(den, 1e-6)


def match_histograms(ref: np.ndarray, ins: np.ndarray, valid: np.ndarray, cfg: CompareConfig) -> tuple[np.ndarray, dict]:
    """Regularised per-channel quantile mapping of `ins` to `ref` (uint8 BGR).

    Raw CDF matching over-stretches sparse histogram tails (a few bright parts), so the
    tone curve is built from quantile pairs inside [q_lo, q_hi], extended linearly
    beyond them with a gain/offset fitted to those pairs, smoothed and made monotone.
    """
    out = np.empty_like(ins)
    m = valid > 0
    qs = np.linspace(cfg.histogram_quantile_range[0], cfg.histogram_quantile_range[1], 99)
    v = np.arange(256, dtype=np.float64)
    shifts = []
    for c in range(ins.shape[2]):
        q_ins = np.percentile(ins[..., c][m], qs)
        q_ref = np.percentile(ref[..., c][m], qs)
        q_ins, idx = np.unique(q_ins, return_index=True)  # np.interp needs increasing x
        q_ref = q_ref[idx]
        if len(q_ins) < 2:
            lut = v.copy()
        else:
            gain, offset = np.polyfit(q_ins, q_ref, 1)
            lut = np.interp(v, q_ins, q_ref)
            lut[v < q_ins[0]] = q_ref[0] + gain * (v[v < q_ins[0]] - q_ins[0])
            lut[v > q_ins[-1]] = q_ref[-1] + gain * (v[v > q_ins[-1]] - q_ins[-1])
            lut = cv2.GaussianBlur(lut.reshape(1, -1), (0, 0), cfg.histogram_lut_smooth).ravel()
            lut = np.maximum.accumulate(lut)
        lut_u8 = np.clip(lut.round(), 0, 255).astype(np.uint8)
        out[..., c] = cv2.LUT(np.ascontiguousarray(ins[..., c]), lut_u8)
        used = np.unique(ins[..., c][m])
        shifts.append(float(np.abs(lut_u8[used].astype(np.int16) - used).mean()))
    return out, {"mode": "histogram", "mean_abs_lut_shift_bgr": [round(s, 2) for s in shifts]}


def normalize_photometric(ref_lab: np.ndarray, ins_lab: np.ndarray, valid: np.ndarray, cfg: CompareConfig) -> tuple[np.ndarray, dict]:
    """Linear global (if configured) and local low-frequency correction in LAB; returns float32."""
    out = ins_lab.astype(np.float32).copy()
    ref = ref_lab.astype(np.float32)
    m = valid > 0
    info: dict = {}
    if cfg.global_mode == "linear" and m.any():
        r_l, i_l = ref[..., 0][m], out[..., 0][m]
        r_q = np.percentile(r_l, [25, 50, 75])
        i_q = np.percentile(i_l, [25, 50, 75])
        gain = float(np.clip((r_q[2] - r_q[0]) / max(i_q[2] - i_q[0], 1e-3), 1 / cfg.max_gain, cfg.max_gain))
        offset = float(r_q[1] - gain * i_q[1])
        out[..., 0] = out[..., 0] * gain + offset
        ab = []
        for c in (1, 2):
            d = float(np.clip(np.median(ref[..., c][m]) - np.median(out[..., c][m]), -cfg.max_ab_offset, cfg.max_ab_offset))
            out[..., c] += d
            ab.append(d)
        info["global"] = {"L_gain": gain, "L_offset": offset, "a_offset": ab[0], "b_offset": ab[1]}

    if cfg.normalize_local and m.any():
        sigma = cfg.local_sigma_frac * max(ref.shape[:2])
        # Exclude strongly differing pixels (likely defects) from the illumination fit.
        excl = (np.abs(ref[..., 0] - out[..., 0]) > cfg.local_exclude_diff).astype(np.uint8)
        excl = cv2.dilate(excl, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
        w = (m & (excl == 0)).astype(np.float32)
        lo, hi = cfg.local_gain_range
        ratio = _masked_blur(ref[..., 0] + 1.0, w, sigma) / np.maximum(_masked_blur(out[..., 0] + 1.0, w, sigma), 1.0)
        ratio = np.clip(ratio, lo, hi)
        out[..., 0] = (out[..., 0] + 1.0) * ratio - 1.0
        info["local"] = {
            "sigma_px": float(sigma),
            "excluded_fraction": float(1.0 - w[m].mean()),
            "gain_min": float(ratio[m].min()),
            "gain_max": float(ratio[m].max()),
        }
    np.clip(out, 0, 255, out=out)
    return out, info


# --------------------------------------------------------------------------- #
# Cues
# --------------------------------------------------------------------------- #
def range_distance(a: np.ndarray, b: np.ndarray, radius: int) -> np.ndarray:
    """Symmetric distance of each value to the other image's local min..max range."""
    if radius <= 0:
        return np.abs(a - b)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * radius + 1, 2 * radius + 1))

    def one_way(x, y):
        return np.maximum(np.maximum(x - cv2.dilate(y, k), cv2.erode(y, k) - x), 0)

    return np.maximum(one_way(a, b), one_way(b, a))


def ssim_map(a: np.ndarray, b: np.ndarray, sigma: float) -> np.ndarray:
    """Local SSIM (Wang et al. 2004) with a Gaussian window, for 0..255 inputs."""
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    blur = lambda x: cv2.GaussianBlur(x, (0, 0), sigma)  # noqa: E731
    mu_a, mu_b = blur(a), blur(b)
    var_a = blur(a * a) - mu_a**2
    var_b = blur(b * b) - mu_b**2
    cov = blur(a * b) - mu_a * mu_b
    return ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a**2 + mu_b**2 + c1) * (var_a + var_b + c2))


def gradient_magnitude(l_chan: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(l_chan, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(l_chan, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def focus_measure(l_chan: np.ndarray, valid: np.ndarray) -> float:
    """Variance of the Laplacian over valid pixels."""
    lap = cv2.Laplacian(l_chan, cv2.CV_32F, ksize=3)
    return float(lap[valid > 0].var()) if np.any(valid) else 0.0


def equalize_sharpness(r: list[np.ndarray], i: list[np.ndarray], valid: np.ndarray, cfg: CompareConfig) -> dict:
    """Blur the sharper image's channels (in place) until focus measures match."""
    fm_r, fm_i = focus_measure(r[0], valid), focus_measure(i[0], valid)
    info = {"focus_reference": round(fm_r, 2), "focus_inspection": round(fm_i, 2), "blurred": None, "sigma": 0.0}
    if not cfg.equalize_sharpness or min(fm_r, fm_i) <= 0:
        return info
    sharp, target, name = (r, fm_i, "reference") if fm_r > fm_i else (i, fm_r, "inspection")
    if max(fm_r, fm_i) / min(fm_r, fm_i) < cfg.sharpness_ratio_trigger:
        return info
    lo, hi = 0.0, cfg.max_equalize_sigma
    for _ in range(12):  # bisection on sigma: focus decreases monotonically with blur
        mid = (lo + hi) / 2
        if focus_measure(cv2.GaussianBlur(sharp[0], (0, 0), mid), valid) > target:
            lo = mid
        else:
            hi = mid
    sigma = hi
    for c in range(3):
        sharp[c] = cv2.GaussianBlur(sharp[c], (0, 0), sigma)
    info.update(blurred=name, sigma=round(sigma, 3), focus_after=round(focus_measure(sharp[0], valid), 2))
    return info


def compute_cues(ref_lab: np.ndarray, ins_lab: np.ndarray, valid: np.ndarray, cfg: CompareConfig) -> tuple[dict[str, np.ndarray], dict]:
    blur = lambda x: cv2.GaussianBlur(x, (0, 0), cfg.blur_sigma) if cfg.blur_sigma > 0 else x  # noqa: E731
    r = [blur(np.ascontiguousarray(ref_lab[..., c], dtype=np.float32)) for c in range(3)]
    i = [blur(np.ascontiguousarray(ins_lab[..., c], dtype=np.float32)) for c in range(3)]
    sharpness = equalize_sharpness(r, i, valid, cfg)
    if cfg.chroma_blur_sigma > 0:  # camera/JPEG chroma is low resolution; compare it at that scale
        for ch in (r, i):
            ch[1] = cv2.GaussianBlur(ch[1], (0, 0), cfg.chroma_blur_sigma)
            ch[2] = cv2.GaussianBlur(ch[2], (0, 0), cfg.chroma_blur_sigma)
    t = cfg.align_tolerance_px
    da, db = range_distance(r[1], i[1], t), range_distance(r[2], i[2], t)
    cues = {
        "intensity": np.clip(range_distance(r[0], i[0], t) / cfg.intensity_scale, 0, 1),
        "ssim": np.clip((1.0 - ssim_map(r[0], i[0], cfg.ssim_sigma)) / cfg.ssim_scale, 0, 1),
        "gradient": np.clip(range_distance(gradient_magnitude(r[0]), gradient_magnitude(i[0]), t) / cfg.gradient_scale, 0, 1),
        "color": np.clip(np.sqrt(da * da + db * db) / cfg.color_scale, 0, 1),
    }
    return cues, sharpness


def fuse(cues: dict[str, np.ndarray], valid: np.ndarray, cfg: CompareConfig) -> np.ndarray:
    total = sum(cfg.weights.get(c, 0.0) for c in CUES)
    if total <= 0:
        raise ValueError("cue weights must sum to a positive value")
    fused = sum(cfg.weights.get(c, 0.0) * cues[c] for c in CUES) / total
    fused = fused.astype(np.float32)
    if cfg.map_smooth_sigma > 0:
        fused = cv2.GaussianBlur(fused, (0, 0), cfg.map_smooth_sigma)
    fused[valid == 0] = 0.0
    return np.clip(fused, 0, 1)


# --------------------------------------------------------------------------- #
# Regions
# --------------------------------------------------------------------------- #
def extract_regions(anomaly: np.ndarray, cues: dict[str, np.ndarray], valid: np.ndarray, ref_l: np.ndarray, ins_l: np.ndarray, cfg: CompareConfig) -> tuple[list[dict], dict]:
    binary = ((anomaly >= cfg.threshold) & (valid > 0)).astype(np.uint8) * 255
    if cfg.open_px > 1:
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg.open_px, cfg.open_px)))
    if cfg.close_px > 1:
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg.close_px, cfg.close_px)))
    binary[valid == 0] = 0

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    min_area = max(1, int(cfg.min_region_area_frac * max(np.count_nonzero(valid), 1)))
    g_ref, g_ins = gradient_magnitude(ref_l), gradient_magnitude(ins_l)
    regions, rejected = [], {"too_small": 0, "low_peak": 0}
    for lbl in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[lbl])
        if area < min_area:
            rejected["too_small"] += 1
            continue
        sel = labels[y : y + h, x : x + w] == lbl
        vals = anomaly[y : y + h, x : x + w][sel]
        peak = float(vals.max())
        if peak < cfg.min_region_peak:
            rejected["low_peak"] += 1
            continue
        evidence = {c: round(float(cues[c][y : y + h, x : x + w][sel].mean()), 4) for c in CUES}
        s_ref = float(g_ref[y : y + h, x : x + w][sel].mean())
        s_ins = float(g_ins[y : y + h, x : x + w][sel].mean())
        evidence.update(edge_strength_reference=round(s_ref, 2), edge_strength_inspection=round(s_ins, 2))
        evidence["mean_L_reference"] = round(float(ref_l[y : y + h, x : x + w][sel].mean()), 2)
        evidence["mean_L_inspection"] = round(float(ins_l[y : y + h, x : x + w][sel].mean()), 2)
        regions.append(
            {
                "bbox_xywh": [x, y, w, h],
                "area_px": area,
                "centroid_xy": [round(float(centroids[lbl][0]), 2), round(float(centroids[lbl][1]), 2)],
                "peak_score": round(peak, 4),
                "mean_score": round(float(vals.mean()), 4),
                "dominant_cue": max(CUES, key=lambda c: evidence[c]),
                "change_hint": _change_hint(s_ref, s_ins),
                "evidence": evidence,
            }
        )
    regions.sort(key=lambda r: (r["mean_score"] * np.sqrt(r["area_px"])), reverse=True)
    dropped = max(0, len(regions) - cfg.max_regions)
    diag = {"min_region_area_px": min_area, "components_before_filter": n - 1, "rejected": rejected, "truncated": dropped}
    return regions[: cfg.max_regions], diag


def _change_hint(edge_ref: float, edge_ins: float) -> str:
    """Heuristic only: which side has more structure inside the region."""
    if edge_ref > 1.5 * edge_ins + 2.0:
        return "structure_removed"
    if edge_ins > 1.5 * edge_ref + 2.0:
        return "structure_added"
    return "appearance_changed"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def compare(ref: np.ndarray, aligned: np.ndarray, valid: np.ndarray, cfg: CompareConfig) -> ComparisonResult:
    if ref.shape != aligned.shape or ref.shape[:2] != valid.shape:
        raise ValueError(f"shape mismatch: ref {ref.shape}, aligned {aligned.shape}, valid {valid.shape}")
    # Fill unobserved pixels with the reference so blur/SSIM windows near the overlap
    # border do not see the black warp padding (they are zeroed in the map anyway).
    aligned = np.where(valid[..., None] > 0, aligned, ref)
    photometric: dict = {}
    if cfg.global_mode == "histogram":
        aligned, photometric["global"] = match_histograms(ref, aligned, valid, cfg)
    elif cfg.global_mode not in ("linear", "none"):
        raise ValueError(f"unknown global_mode {cfg.global_mode!r}")
    ref_lab = cv2.cvtColor(ref, cv2.COLOR_BGR2LAB)
    ins_lab, lab_info = normalize_photometric(ref_lab, cv2.cvtColor(aligned, cv2.COLOR_BGR2LAB), valid, cfg)
    photometric.update(lab_info)
    cues, sharpness = compute_cues(ref_lab, ins_lab, valid, cfg)
    anomaly = fuse(cues, valid, cfg)
    ref_l = ref_lab[..., 0].astype(np.float32)
    regions, region_diag = extract_regions(anomaly, cues, valid, ref_l, ins_lab[..., 0], cfg)

    m = valid > 0
    diag = {
        "photometric": photometric,
        "sharpness": sharpness,
        "valid_fraction": float(m.mean()),
        "cue_mean": {c: round(float(cues[c][m].mean()), 4) for c in CUES},
        "cue_p99": {c: round(float(np.percentile(cues[c][m], 99)), 4) for c in CUES},
        "anomaly_p99": round(float(np.percentile(anomaly[m], 99)), 4) if m.any() else 0.0,
        "anomaly_max": round(float(anomaly.max()), 4),
        "fraction_above_threshold": round(float((anomaly[m] >= cfg.threshold).mean()), 5) if m.any() else 0.0,
        "regions": region_diag,
    }
    return ComparisonResult(anomaly, cues, regions, diag)
