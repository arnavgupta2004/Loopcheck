"""Reference-to-inspection registration.

Coordinate convention (used everywhere in LoopCheck):

    H maps REFERENCE pixel coordinates to INSPECTION pixel coordinates,
        p_insp ~ H @ p_ref        (homogeneous)

Consequences, verified in tests/test_registration.py:
  * The inspection image in reference coordinates is
        cv2.warpPerspective(insp, H, ref_size, flags=INTER_LINEAR | WARP_INVERSE_MAP)
    because with WARP_INVERSE_MAP OpenCV computes dst(x) = src(H x).
  * cv2.findTransformECC(template=ref, input=insp, warp) returns W such that
    ref(x) ~ insp(W x), i.e. W follows the same convention, so an estimated H can be
    passed in directly as the initial warp and the result used directly.
  * A box in reference coordinates is drawn on the inspection image by projecting its
    corners with cv2.perspectiveTransform(pts, H).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import RegistrationConfig


@dataclass
class RegistrationResult:
    ok: bool
    method: str | None
    H: np.ndarray | None
    metrics: dict = field(default_factory=dict)
    failure_reason: str | None = None
    attempts: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def to_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def project_points(pts: np.ndarray, H: np.ndarray) -> np.ndarray:
    """Map Nx2 reference points into inspection coordinates."""
    return cv2.perspectiveTransform(np.asarray(pts, np.float64).reshape(-1, 1, 2), H).reshape(-1, 2)


def frame_corners(shape: tuple[int, ...]) -> np.ndarray:
    h, w = shape[:2]
    return np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float64)


def corner_error(H_est: np.ndarray, H_true: np.ndarray, ref_shape: tuple[int, ...]) -> float:
    """Mean distance (inspection px) between the reference corners mapped by both homographies."""
    c = frame_corners(ref_shape)
    return float(np.linalg.norm(project_points(c, H_est) - project_points(c, H_true), axis=1).mean())


def warp_to_reference(insp: np.ndarray, H: np.ndarray, ref_shape: tuple[int, ...], interpolation: int = cv2.INTER_LINEAR) -> np.ndarray:
    h, w = ref_shape[:2]
    return cv2.warpPerspective(insp, H, (w, h), flags=interpolation | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def overlap_mask(insp_shape: tuple[int, ...], H: np.ndarray, ref_shape: tuple[int, ...], erode_px: int = 0) -> np.ndarray:
    """uint8 mask (255 = valid) of reference pixels that land inside the inspection image."""
    ones = np.full(insp_shape[:2], 255, np.uint8)
    mask = warp_to_reference(ones, H, ref_shape, cv2.INTER_NEAREST)
    if erode_px > 0:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * erode_px + 1, 2 * erode_px + 1))
        mask = cv2.erode(mask, k, borderType=cv2.BORDER_CONSTANT, borderValue=0)
    return mask


def scale_homography(H: np.ndarray, s_ref: float, s_insp: float) -> np.ndarray:
    """Convert H between resolutions: H' = S_insp H S_ref^-1 for p' = s p."""
    S_r = np.diag([s_ref, s_ref, 1.0])
    S_i = np.diag([s_insp, s_insp, 1.0])
    return S_i @ H @ np.linalg.inv(S_r)


def sanity_check(H: np.ndarray, ref_shape, insp_shape, cfg: RegistrationConfig) -> tuple[str | None, dict]:
    """Return (failure reason or None, metrics) for a candidate homography."""
    metrics: dict = {}
    if H is None or not np.all(np.isfinite(H)) or abs(H[2, 2]) < 1e-12:
        return "degenerate_homography", metrics
    H = H / H[2, 2]
    det = float(np.linalg.det(H[:2, :2]))
    metrics["scale"] = float(np.sqrt(abs(det)))
    if det <= 0:
        return "orientation_flip", metrics
    if not (1.0 / cfg.max_scale_change <= metrics["scale"] <= cfg.max_scale_change):
        return "implausible_scale", metrics
    poly = project_points(frame_corners(ref_shape), H).astype(np.float32)
    if not cv2.isContourConvex(poly.reshape(-1, 1, 2)):
        return "non_convex_projection", metrics
    mask = overlap_mask(insp_shape, H, ref_shape)
    metrics["overlap_ratio"] = float(np.count_nonzero(mask)) / mask.size
    if metrics["overlap_ratio"] < cfg.min_overlap:
        return "insufficient_overlap", metrics
    return None, metrics


# --------------------------------------------------------------------------- #
# ArUco path
# --------------------------------------------------------------------------- #
def detect_aruco(gray: np.ndarray, dictionary_name: str) -> dict[int, np.ndarray]:
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary_name))
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    corners, ids, _ = cv2.aruco.ArucoDetector(dictionary, params).detectMarkers(gray)
    if ids is None:
        return {}
    out: dict[int, np.ndarray] = {}
    for c, i in zip(corners, ids.ravel()):
        out.setdefault(int(i), c.reshape(4, 2).astype(np.float64))  # first instance only
    return out


def estimate_aruco(ref_gray, insp_gray, cfg: RegistrationConfig) -> dict:
    ref_m = detect_aruco(ref_gray, cfg.aruco_dictionary)
    insp_m = detect_aruco(insp_gray, cfg.aruco_dictionary)
    common = sorted(set(ref_m) & set(insp_m))
    attempt = {"method": "aruco", "markers_reference": len(ref_m), "markers_inspection": len(insp_m), "markers_common": len(common)}
    if len(common) < cfg.aruco_min_markers:
        attempt["failure_reason"] = "too_few_common_markers"
        return attempt
    src = np.concatenate([ref_m[i] for i in common])
    dst = np.concatenate([insp_m[i] for i in common])
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, cfg.aruco_max_reproj_px)
    if H is None:
        attempt["failure_reason"] = "degenerate_homography"
        return attempt
    err = np.linalg.norm(project_points(src, H) - dst, axis=1)
    attempt.update(
        H=H,
        inliers=int(mask.sum()),
        correspondences=len(src),
        inlier_ratio=float(mask.mean()),
        reproj_error_px=float(err.mean()),
    )
    if attempt["reproj_error_px"] > cfg.aruco_max_reproj_px:
        attempt["failure_reason"] = "reprojection_error_too_high"
    return attempt


# --------------------------------------------------------------------------- #
# SIFT path
# --------------------------------------------------------------------------- #
def estimate_sift(ref_gray, insp_gray, cfg: RegistrationConfig) -> dict:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    sift = cv2.SIFT_create(nfeatures=cfg.sift_nfeatures)
    kp_r, des_r = sift.detectAndCompute(clahe.apply(ref_gray), None)
    kp_i, des_i = sift.detectAndCompute(clahe.apply(insp_gray), None)
    attempt = {"method": "sift", "keypoints_reference": len(kp_r), "keypoints_inspection": len(kp_i)}
    if des_r is None or des_i is None or len(kp_r) < 4 or len(kp_i) < 4:
        attempt["failure_reason"] = "too_few_keypoints"
        return attempt
    knn = cv2.BFMatcher(cv2.NORM_L2).knnMatch(des_r, des_i, k=2)
    good = [p[0] for p in knn if len(p) == 2 and p[0].distance < cfg.sift_ratio * p[1].distance]
    attempt["ratio_test_matches"] = len(good)
    if len(good) < max(cfg.min_inliers, 4):
        attempt["failure_reason"] = "too_few_matches"
        return attempt
    src = np.float64([kp_r[m.queryIdx].pt for m in good])
    dst = np.float64([kp_i[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, cfg.usac_threshold_px, maxIters=10000, confidence=0.999)
    if H is None:
        attempt["failure_reason"] = "degenerate_homography"
        return attempt
    inl = mask.ravel().astype(bool)
    err = np.linalg.norm(project_points(src[inl], H) - dst[inl], axis=1)
    attempt.update(
        H=H,
        inliers=int(inl.sum()),
        inlier_ratio=float(inl.mean()),
        reproj_error_px=float(err.mean()) if len(err) else float("inf"),
    )
    if attempt["inliers"] < cfg.min_inliers:
        attempt["failure_reason"] = "too_few_inliers"
    elif attempt["inlier_ratio"] < cfg.min_inlier_ratio:
        attempt["failure_reason"] = "low_inlier_ratio"
    elif attempt["reproj_error_px"] > cfg.max_reproj_px:
        attempt["failure_reason"] = "reprojection_error_too_high"
    return attempt


# --------------------------------------------------------------------------- #
# ECC refinement
# --------------------------------------------------------------------------- #
def refine_ecc(ref_gray, insp_gray, H: np.ndarray, cfg: RegistrationConfig) -> tuple[np.ndarray, dict]:
    """Refine H with ECC at reduced resolution; returns (H, info). H is unchanged on rejection."""
    s_r = min(1.0, cfg.ecc_max_dim / max(ref_gray.shape))
    s_i = min(1.0, cfg.ecc_max_dim / max(insp_gray.shape))
    ref_s = cv2.resize(ref_gray, None, fx=s_r, fy=s_r, interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    ins_s = cv2.resize(insp_gray, None, fx=s_i, fy=s_i, interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    H0 = scale_homography(H, s_r, s_i).astype(np.float32)
    mask = overlap_mask(ins_s.shape, H0, ref_s.shape, erode_px=2)
    info: dict = {"attempted": True, "accepted": False}
    try:
        cc0 = float(cv2.computeECC(ref_s, warp_to_reference(ins_s, H0, ref_s.shape), mask))
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, cfg.ecc_iterations, cfg.ecc_eps)
        # inputMask is in *input* (inspection) coordinates and warped internally; None
        # means "all valid", and ECC already ignores pixels warped from outside the input.
        cc1, W = cv2.findTransformECC(ref_s, ins_s, H0.copy(), cv2.MOTION_HOMOGRAPHY, criteria, None, cfg.ecc_gauss_size)
    except cv2.error as exc:
        info["rejected_reason"] = f"ecc_error: {str(exc).strip().splitlines()[-1][:160]}"
        return H, info
    H1 = scale_homography(W.astype(np.float64), 1.0 / s_r, 1.0 / s_i)
    shift = float(np.linalg.norm(project_points(frame_corners(ref_gray.shape), H1) - project_points(frame_corners(ref_gray.shape), H), axis=1).max())
    info.update(cc_before=cc0, cc_after=float(cc1), max_corner_shift_px=shift)
    if not np.all(np.isfinite(H1)):
        info["rejected_reason"] = "non_finite"
    elif cc1 <= cc0:
        info["rejected_reason"] = "no_correlation_gain"
    elif shift > cfg.ecc_max_corner_shift_px:
        info["rejected_reason"] = "corner_shift_too_large"
    else:
        info["accepted"] = True
        return H1 / H1[2, 2], info
    return H, info


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def register(ref: np.ndarray, insp: np.ndarray, cfg: RegistrationConfig) -> RegistrationResult:
    """Estimate H (reference -> inspection) with ArUco, falling back to SIFT; gate the result."""
    ref_gray, insp_gray = to_gray(ref), to_gray(insp)
    attempts: list[dict] = []
    chosen: dict | None = None

    for estimator in (estimate_aruco, estimate_sift):
        attempt = estimator(ref_gray, insp_gray, cfg)
        if "H" in attempt and "failure_reason" not in attempt:
            reason, sanity = sanity_check(attempt["H"], ref.shape, insp.shape, cfg)
            attempt.update(sanity)
            if reason:
                attempt["failure_reason"] = reason
        attempts.append(attempt)
        if "failure_reason" not in attempt:
            chosen = attempt
            break

    public = [{k: v for k, v in a.items() if k != "H"} for a in attempts]
    if chosen is None:
        return RegistrationResult(False, None, None, failure_reason=attempts[-1]["failure_reason"], attempts=public)

    H = chosen["H"] / chosen["H"][2, 2]
    metrics = {k: v for k, v in chosen.items() if k not in ("H", "method")}
    if cfg.ecc_enabled:
        H, ecc_info = refine_ecc(ref_gray, insp_gray, H, cfg)
        metrics["ecc"] = ecc_info
        if ecc_info["accepted"]:
            reason, sanity = sanity_check(H, ref.shape, insp.shape, cfg)
            if reason:  # should not happen given the corner-shift bound, but never trust it blindly
                return RegistrationResult(False, chosen["method"], None, metrics, f"after_ecc_{reason}", public)
            metrics.update(sanity)
    else:
        metrics["ecc"] = {"attempted": False}
    return RegistrationResult(True, chosen["method"], H, metrics, None, public)
