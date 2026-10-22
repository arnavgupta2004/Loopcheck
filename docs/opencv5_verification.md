# OpenCV 5 verification (D1)

Run: `uv run --frozen python scripts/check_opencv5.py --json results/opencv5_check.json`

Environment: Python 3.13.16, Linux x86_64, numpy 2.5.4,
`opencv-contrib-python-headless==5.0.0.93` (`cv2.__version__ == "5.0.0"`), the only
OpenCV distribution installed. Wheel built with pthreads, OpenBLAS LAPACK,
SIMD dispatch up to AVX512_SKX. The `Host` and `Python 3 Interpreter` lines in
the build info describe the wheel's build machine (abi3 wheel built with
CPython 3.9), not this environment.

Every check executes a real operation on synthetic images and validates the
result against known ground truth. Results were identical across three runs,
including one from a fresh `uv sync --frozen`.

| Check | APIs exercised | Result |
|---|---|---|
| Version | `cv2.__version__` | 5.0.0 |
| ArUco | `aruco.getPredefinedDictionary`, `generateImageMarker`, `DetectorParameters` (`CORNER_REFINE_SUBPIX`), `ArucoDetector.detectMarkers`, `findHomography` | 4/4 ids found under a perspective warp; homography corner error 0.67 px |
| SIFT | `SIFT_create`, `detectAndCompute`, `BFMatcher.knnMatch` + ratio test | 328 / 596 keypoints, 128-d float32 descriptors, 184 matches, 88% geometrically correct |
| USAC / MAGSAC | `findHomography` with `USAC_MAGSAC`, `USAC_DEFAULT`, `RANSAC`; `UsacParams` object | With 73 injected gross outliers (28%): corner error 0.15 / 0.14 / 0.13 px; `UsacParams` path 0.15 px |
| ECC | `findTransformECC` (`MOTION_HOMOGRAPHY`, `MOTION_AFFINE`, `gaussFiltSize=5`), `computeECC` | Homography: cc 0.99975, corner error 0.30 px. Affine: shift error 0.07 px |
| Template matching | `matchTemplate` (`TM_CCOEFF_NORMED`, `TM_SQDIFF_NORMED`, masked `TM_CCORR_NORMED`), `minMaxLoc` | Exact location in all modes; displaced copy with +25 brightness found exactly (score 0.99999) |
| Filtering / thresholds / morphology | `GaussianBlur`, `Sobel`, `magnitude`, Otsu `threshold`, `adaptiveThreshold`, `morphologyEx` open/close, `createCLAHE`, `cvtColor` BGR→LAB | All outputs matched expected values (Otsu foreground 9,989 px vs 10,000 true) |
| Contours / components | `findContours` (returns 2 values), `boundingRect`, `contourArea`, `connectedComponentsWithStats` | 4 contours, 5 labels incl. background, exact rectangle areas |
| Mini end-to-end | SIFT → MAGSAC → `warpPerspective(WARP_INVERSE_MAP)` → valid-overlap mask → `absdiff` → threshold → components | Synthetic "missing component" found with IoU 1.0 |

## Differences from OpenCV 4 observed

- **No API failures.** Every function and constant planned for the MVP exists
  under its 4.x Python name and behaved as expected. No substitutes were needed.
- **Modules are reorganised in C++:** the build lists `features`, `geometry`,
  `calib`, `stereo` and `ptcloud` instead of 4.x's `features2d` and `calib3d`.
  The Python bindings keep the flat `cv2.*` namespace for everything tested.
- **Conventions confirmed by test, not assumed:**
  - `findTransformECC(template, input, ...)` returns W with template(x) ≈ input(W·x).
    Warping the inspection image back to the reference therefore needs
    `WARP_INVERSE_MAP`.
  - ArUco marker corners are reported at pixel-edge positions. Expect
    sub-pixel offsets against integer pixel coordinates (0.67 px here).

## Not covered by D1

Real photographs, lighting robustness, low-texture or repetitive scenes,
performance on Lambda/arm64, and the aarch64 wheel. These are evaluated in later steps.
