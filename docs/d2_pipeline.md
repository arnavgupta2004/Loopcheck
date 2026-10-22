# D2: Deterministic comparison pipeline

Everything here is classical OpenCV 5 (no learned model, no external service).
Scores are uncalibrated heuristics in [0, 1], **not probabilities**.

## Flow

```
load + validate (imageio)  ->  downscale to work_max_dim (1024)
  -> register (registration):  ArUco (>=3 common ids)  --fail-->  SIFT + ratio test + USAC_MAGSAC
                               -> quality gate -> [optional ECC] -> H (ref -> insp)
     gate fails  ->  status = registration_failed, no regions, no verdict
  -> align: warpPerspective(insp, H, WARP_INVERSE_MAP) + valid-overlap mask (eroded 4 px)
  -> compare (compare):  photometric normalisation -> sharpness equalisation
                         -> 4 cues -> weighted fusion -> threshold + morphology
                         -> connected components -> filtered regions + evidence
  -> report.json, overlay.png (inspection image), heatmap.png, aligned.png, anomaly_map.png
```

## Coordinate convention

`H` maps **reference px -> inspection px** (`p_insp ~ H p_ref`). All region boxes are in
reference px (`bbox_xywh_reference`); `polygon_inspection` is the same box projected
into the inspection image with `perspectiveTransform(pts, H)`.

Verified by tests (`tests/test_registration.py`):
- `warpPerspective(insp, H, ref_size, flags=INTER_LINEAR | WARP_INVERSE_MAP)` brings the
  inspection image into the reference frame (dst(x) = src(H x)). The same H without the
  flag warps in the wrong direction; the test checks both.
- `findTransformECC(template=ref, input=insp, W)` returns W with ref(x) ~ insp(W x),
  i.e. the same convention, so H can seed ECC directly.

## Registration quality gate

| Check | Default | Failure reason |
|---|---|---|
| Common ArUco ids (else try SIFT) | >= 3 | `too_few_common_markers` |
| ArUco corner reprojection error | <= 2.0 px | `reprojection_error_too_high` |
| SIFT ratio-test matches / MAGSAC inliers | >= 30 / >= 30 | `too_few_matches` / `too_few_inliers` |
| Inlier ratio | >= 0.30 | `low_inlier_ratio` |
| Mean inlier reprojection error | <= 1.5 px | `reprojection_error_too_high` |
| Scale sqrt(det A), orientation, convex frame projection | [0.5, 2], det > 0, convex | `implausible_scale`, `orientation_flip`, `non_convex_projection` |
| Reference area visible in inspection | >= 0.60 | `insufficient_overlap` |

**ECC refinement** is implemented and tested, but **off by default** (`--ecc` enables it).
It runs only after the gate passes. Its result is kept only if correlation improves and
no frame corner moves more than 4 px. On the dev split it *increased* the median SIFT
corner error from 0.06 to 0.13 px (max 0.97 px), mostly in cases with spatially varying
illumination, which biases ECC (it is invariant only to global gain/offset).

## Comparison cues

The inspection image is normalised to the reference before comparison:
1. A robust **global** fit: L gain/offset from median and IQR, plus median a/b offsets.
2. A **local** low-frequency L correction: a masked normalised convolution, sigma = 8% of
   the image size, excluding pixels that differ by more than 25 L, with the gain clamped
   to [0.7, 1.4].
3. **Sharpness equalisation**: if the variance-of-Laplacian ratio exceeds 1.5, the sharper
   image is blurred, with the sigma found by bisection, until the two match.

| Cue | Definition | Scale (cue = 1) | Weight |
|---|---|---|---|
| intensity | misalignment-tolerant L difference | 45 L | 0.30 |
| ssim | (1 - local SSIM on L, Gaussian sigma 2) | 0.6 | 0.20 |
| gradient | tolerant Sobel-magnitude difference | 90 | 0.20 |
| color | tolerant a/b distance (a/b pre-blurred sigma 2) | 22 | 0.30 |

"Tolerant" means a pixel counts only if its value lies outside the other image's
min..max in a 3x3 neighbourhood, in both directions. This removes sub-pixel
registration residue on edges but keeps solid or thin changes (unit-tested).

The fused map (weighted mean, smoothed with sigma 1.5) is thresholded at 0.30, then
opened (3 px) and closed (9 px). Connected components are kept if their area is at
least 0.03% of the valid area and their peak score is at least 0.45. Each region
reports its peak and mean score, mean per-cue evidence, dominant cue, edge strength and
mean L on both sides, and a heuristic `change_hint` (`structure_removed` /
`structure_added` / `appearance_changed`). That hint is not a classification.

All values live in `loopcheck/config.py` and can be overridden with
`--config file.json`, e.g. `{"compare": {"threshold": 0.35}}`.

## Synthetic data

`loopcheck.synth` renders tray scenes: 12 slots, six component kinds and random colours,
with ArUco markers in about 50% of cases. Scenes are drawn on a canvas with a margin, so
perspective changes reveal real background. Seeds come from
`SeedSequence([seed, type, index])`, and the same seed gives byte-identical PNGs (tested).

| Category | Case types | Ground truth |
|---|---|---|
| nominal | identical, lighting (gain/offset/gamma/colour cast/illumination gradient), geometric (perspective + translation), blur_noise (blur + noise + JPEG 70), combined_nominal | no defect |
| defect | missing, shifted (two GT boxes: old and new position), foreign_object, missing_hard (strong lighting + perspective), subtle_foreign (7-10 px low-contrast object, ambiguous) | boxes in reference px |
| registration_fail | unrelated_scene, low_overlap (~50% shift), heavy_blur (sigma 7) | expected `registration_failed` |

Defect cases also get mild perturbations (8 px corner jitter, 30% photometric
strength, blur 0.5, noise 2, JPEG 90).

## Evaluation protocol

- **Dev split:** seed 1000, 8 per type, 104 cases. Every design decision and threshold
  was made by looking only at this split.
- **Test split:** seed 2000, 10 per type, 130 cases. Generated and evaluated once, after
  the configuration was frozen and committed (commit `33654e5`). It was not used for tuning.

Dev-split decisions, from measured sweeps:

| Change | Reason (dev split) |
|---|---|
| Added sharpness equalisation | blur_noise false positives: focus measure 902 vs 334 |
| Global normalisation: linear instead of histogram matching | Histogram matching FPR 0.125 vs linear 0.025 under noise + JPEG |
| Added chroma pre-blur | JPEG 4:2:0 colour bleeding on thin stripes |
| Kept 3x3 misalignment tolerance | Removing it raised FPR from 0.025 to 0.075 |
| Kept threshold 0.30 | 0.27 / 0.30 / 0.33 all gave FPR 0.025 and F1 0.95–0.96 (a plateau, not a knife edge) |
| ECC off by default | It worsened corner error (see above) |

## Results

Generated files: `results/eval_dev/` and `results/eval_test/` (`summary.md`,
`metrics.json`, `per_case.json`, `gallery/`). Held-out test split, OpenCV 5.0.0:

| Metric | Test (130 cases) | Dev (104 cases) |
|---|---|---|
| Image-level precision / recall / F1 | 0.941 / 0.960 / 0.950 | 0.974 / 0.925 / 0.949 |
| FPR on nominal images (incl. lighting, blur, perspective) | 0.06 (3/50) | 0.025 (1/40) |
| Region recall @ IoU >= 0.3 | 0.933 | 0.896 |
| Region precision @ IoU >= 0.3 (defect cases) | 0.758 | 0.949 |
| Registration success, should-register cases | 1.000 (56 ArUco / 44 SIFT) | 1.000 (29 ArUco / 51 SIFT) |
| Corner error vs true H (median / p90 / max) | 0.064 / 0.115 / 0.174 px | 0.058 / 0.118 / 0.170 px |
| Correct rejection of registration-fail cases | 1.000 (30/30) | 1.000 (24/24) |
| Median time per case (800x600, 4-core CPU) | 0.32 s | |

These numbers come from synthetic data only. They show the pipeline works as designed
on controlled perturbations; they do not predict performance on real photographs.

## Known failure cases (test split)

1. **White ArUco quiet zones under gamma change** (`lighting_03`, `_08`, `_09`: all 3 test
   false positives). Gamma > 1.08 with a negative offset maps saturated white (L≈255)
   to about 235. The linear tone fit is dominated by mid-tones and can't follow the curve
   at the extremes, so the four marker patches light up through the intensity cue.
   Candidate fix for D3+: mask the detected marker quads out of the comparison, or use
   a monotone tone curve. It must be evaluated on a new split, not this one.
2. **Tiny low-contrast foreign objects** (`subtle_foreign_00`, `_02`: both false negatives).
   These are 7-10 px radius at ±25-40 grey levels; the fused score stays below the
   region thresholds. This is the expected sensitivity limit at the current threshold.
3. **Short shifts merge into one region** (`shifted_04`). The old and new positions
   overlap, so one box covers both and its IoU against each GT box is 0.29. The image
   is still flagged correctly; only the localisation metric misses it.
4. **Extra regions alongside true defects** (region precision 0.758; `missing_hard`
   averages 2.4 regions). Checked case by case: 12 of the 14 extra regions in
   `missing_hard` are the **black ArUco marker interiors** (L = 0 in the reference,
   25–32 in the inspection) in two cases with strong tone changes. This is the same root
   cause as failure 1: marker pixels sit at the tonal extremes, where the linear fit is
   wrong. The other 2 are a dark component and a background edge strip.
5. **By design, heavy blur is rejected at registration** (SIFT finds too few matches or
   inliers). There's no separate image-quality gate yet, so a moderately blurred image
   that still registers is analysed with sharpness equalisation.
6. **ArUco identifies the fixture, not its contents.** A different tray carrying the same
   marker ids would register successfully, and every changed slot would then become a
   candidate region. The synthetic `unrelated_scene` cases have no markers, so they
   don't test this.
