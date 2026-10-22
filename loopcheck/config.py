"""Configuration for registration and comparison.

All pixel quantities are in *working* resolution: both images are downscaled so
their longest side is at most ``PipelineConfig.work_max_dim`` before analysis.
Scores are uncalibrated heuristics in [0, 1], not probabilities.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class RegistrationConfig:
    # ArUco path: used when at least `aruco_min_markers` ids are found in both images.
    aruco_dictionary: str = "DICT_4X4_50"
    aruco_min_markers: int = 3
    aruco_max_reproj_px: float = 2.0

    # SIFT fallback.
    sift_nfeatures: int = 4000
    sift_ratio: float = 0.75
    usac_threshold_px: float = 3.0
    min_inliers: int = 30
    min_inlier_ratio: float = 0.30
    max_reproj_px: float = 1.5  # mean reprojection error of inliers

    # Geometric sanity: reject degenerate or implausible homographies.
    max_scale_change: float = 2.0  # sqrt(|det A|) must lie in [1/x, x]
    min_overlap: float = 0.60  # fraction of reference pixels visible in the inspection image

    # ECC refinement: only attempted after the gate passes, kept only if it improves
    # correlation and does not move the frame corners more than ecc_max_corner_shift_px.
    # Off by default: on the dev split it raised the median corner error of SIFT
    # registrations from 0.06 to 0.13 px, because spatially varying illumination biases
    # the ECC objective (ECC is only invariant to global gain/offset).
    ecc_enabled: bool = False
    ecc_max_dim: int = 640
    ecc_iterations: int = 100
    ecc_eps: float = 1e-5
    ecc_gauss_size: int = 5
    ecc_max_corner_shift_px: float = 4.0

    border_erode_px: int = 4  # shrink the valid-overlap mask to drop interpolation seams


@dataclass
class CompareConfig:
    blur_sigma: float = 1.0
    # Misalignment tolerance: a pixel only counts as different if its value lies outside
    # the min..max range of the other image within this radius (applied symmetrically).
    align_tolerance_px: int = 1

    # Photometric normalisation of the aligned inspection image.
    # "linear":    robust median/IQR gain-offset on LAB L plus median offset on a/b (default).
    # "histogram": regularised per-BGR-channel quantile mapping; models gamma/white balance
    #              but was worse on the dev split under noise + JPEG (FPR 0.125 vs 0.025).
    # "none":      no global correction.
    global_mode: str = "linear"
    histogram_quantile_range: tuple[float, float] = (1.0, 99.0)
    histogram_lut_smooth: float = 6.0  # Gaussian sigma (grey levels) applied to the tone curve
    max_gain: float = 2.0  # linear mode only
    max_ab_offset: float = 25.0  # linear mode only
    normalize_local: bool = True  # low-frequency L correction via masked normalised convolution
    local_sigma_frac: float = 0.08  # Gaussian sigma as a fraction of the longest side
    local_exclude_diff: float = 25.0  # pixels differing more than this are excluded from the fit
    local_gain_range: tuple[float, float] = (0.7, 1.4)

    # Sharpness equalisation: if one image is much sharper (variance of Laplacian after
    # the pre-blur), blur it until the focus measures match, so focus/JPEG differences do
    # not register as structural change. The applied sigma is reported.
    equalize_sharpness: bool = True
    sharpness_ratio_trigger: float = 1.5
    max_equalize_sigma: float = 3.0

    # Cue scales: difference value that maps to cue = 1.0.
    intensity_scale: float = 45.0  # L units (OpenCV 8-bit LAB, 0..255)
    gradient_scale: float = 90.0  # Sobel magnitude units
    color_scale: float = 22.0  # a/b chroma distance
    chroma_blur_sigma: float = 2.0  # extra blur on a/b before the colour cue
    ssim_sigma: float = 2.0
    ssim_scale: float = 0.6  # (1 - SSIM) that maps to cue = 1.0

    weights: dict[str, float] = field(
        default_factory=lambda: {"intensity": 0.30, "ssim": 0.20, "gradient": 0.20, "color": 0.30}
    )
    map_smooth_sigma: float = 1.5

    threshold: float = 0.30
    open_px: int = 3
    close_px: int = 9
    min_region_area_frac: float = 0.0003  # of the valid area
    min_region_peak: float = 0.45
    max_regions: int = 25


@dataclass
class PipelineConfig:
    work_max_dim: int = 1024
    registration: RegistrationConfig = field(default_factory=RegistrationConfig)
    compare: CompareConfig = field(default_factory=CompareConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineConfig":
        cfg = cls()
        for key, value in data.items():
            if key == "registration":
                _update(cfg.registration, value)
            elif key == "compare":
                _update(cfg.compare, value)
            elif key == "work_max_dim":
                cfg.work_max_dim = int(value)
            else:
                raise ValueError(f"unknown config key: {key}")
        return cfg

    @classmethod
    def from_json(cls, path: str | Path) -> "PipelineConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))


def _update(obj, values: dict) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown config key for {type(obj).__name__}: {key}")
        if key in ("local_gain_range", "histogram_quantile_range"):
            value = tuple(value)
        setattr(obj, key, value)
