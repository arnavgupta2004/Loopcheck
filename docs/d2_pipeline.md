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
  -> [D2.1] marker exclusion (markers): detected ArUco quads + margin, from both images
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

---

# D2.1: ArUco marker exclusion

**Motivation (from the D2 test split):** all 3 D2 test false positives were white marker
quiet zones under gamma changes, and 12 of 14 extra boxes in `missing_hard` were black
marker interiors. Marker pixels sit at pure black/white, where the linear tone fit is
least accurate, and they are fixture, not inspected content.

## Method (`loopcheck/markers.py`, `compare(..., exclude=)`)

1. **Detections only.** `register()` already runs ArUco detection on both images. Every
   marker actually detected in either image is masked, whichever method (ArUco or SIFT)
   finally registered the pair. No marker positions are inferred or extrapolated.
2. **Polygon.** Each quad's edges are offset outward by `margin_frac * side + margin_px`
   (default 0.25 × side + 2 px; 16 px for a 56 px marker). This is exact under
   perspective because it offsets edges, not corners. A 4×4 marker is 6 modules wide,
   so a standard 1-module quiet zone is side/6 = 0.167 × side.
3. **Coordinates.** Reference detections are drawn directly. Inspection detections are
   drawn in inspection px and brought into the reference frame with
   `warp_to_reference` (the image's own warp, `WARP_INVERSE_MAP` with H). ArUco corners
   use the pixel-centre convention: the outer marker edge is at x − 0.5, verified to
   0.09 px. On a perspective case, inspection corners mapped back with H⁻¹ agree
   with the reference corners to < 0.2 px; this disagreement is logged per report.
4. **Exclusion without invented content (`exclude_mode="dilate"`).** Excluded pixels
   are removed from the photometric fit and from scoring. The exclusion is grown by
   `exclude_leak_px` = 6 px (about 3 × SSIM sigma) so marker contrast can't reach scored
   pixels through the blur, SSIM or smoothing windows. Both images are left untouched.
   The effective exclusion therefore reaches 22 px past the marker edge (12 px past
   a 10 px quiet zone). On the synthetic 800×600 tray with four 56 px markers that is
   8.6% of the valid area (median).
5. **Safety caps.**
   - A marker whose grown polygon exceeds 3% of the image is not masked
     (`polygon_exceeds_max_marker_area`).
   - If the *effective* (dilated) exclusion exceeds 10% of the valid area, masking is
     skipped entirely (`total_masked_area_exceeds_cap`), falling back to D2 behaviour.
   - No masking happens on registration failure, so there is never a verdict there.
6. **Logging.** `report.json["marker_masking"]` lists every marker:
   - id, source image, side, polygon in reference px, area, masked or not and why;
   - masked and effective area, the cap-check area, and the corner disagreement.

   The overlay draws masked polygons in grey, and the heatmap darkens excluded pixels.

### Alternatives measured on the dev split and rejected

| Variant (dev split, seed 3100) | FPR on good images | Marker-attrib. FP regions | Why rejected |
|---|---|---|---|
| No masking (baseline) | 0.104 | 16 | — |
| Fill excluded pixels with reference values (first attempt) | 0.083 (margin 0.25) | 16 (counted with an 8 px attribution zone; widened to 20 px after this) | A seam at the mask edge; normalisation turns it into gradient/SSIM responses just outside the mask (non-monotonic in margin, which exposed the bug) |
| Inpaint both images (`cv2.inpaint`, Telea), margin 0.25 / 0.35 | 0.083 / 0.062 | 1 / 1 | The fill boundary cuts through nearby structure (the tray edge is 4 px outside the quiet zone), which is reconstructed differently in a sharp vs a blurred image |
| Dilate 6 px, margin 0.17 (inside the quiet zone) | 0.062 | 0 | Extra blur false positive; the margin doesn't cover a full quiet zone |
| Dilate 4 px, margin 0.25 | 0.062 | 0 | Extra blur false positive |
| **Dilate 6 px, margin 0.25 (chosen)** | **0.042** | **0** | Same result as margin 0.35 while excluding less area |

`exclude_mode="inpaint"` stays available in the config but is not the default.

## Fresh evaluation (all SYNTHETIC)

New splits with different seeds; the D2 splits (1000, 2000) are not reused:
- **dev:** seed 3100, 8 per type, 136 cases.
- **test:** seed 3200, 10 per type, 170 cases.

The case set is the 13 D2 types plus four new ones:
- `gamma_lighting`: good images with gamma up to ±0.30, colour cast and illumination
  gradient; markers always present.
- `missing_near_marker`: the component nearest a marker is removed. With this tray
  layout these sit 32–53 px from the quiet zone (dev and test), or about 136–140 px when
  the corner slot is empty. Most are therefore "distant" under the ≤ 40 px rule.
- `foreign_near_marker`: an object 2–25 px outside a quiet zone.
- `different_tray`: different contents with the same marker ids and positions.

"Near" is defined geometrically per ground-truth box: a gap of ≤ 40 px to the nearest
marker quiet zone. A predicted region is *marker-attributable* when it is unmatched
(IoU < 0.3 to every GT box) and ≥ 25% of it lies within 20 px of a marker zone.

**Protocol.**
1. All design choices were made on dev only.
2. The configuration was frozen and committed (`ed43fbd`) before the test split was
   generated.
3. The test split was evaluated once per configuration.
4. Both configurations ran on identical cases.

**Baseline preservation.** With `configs/baseline_no_marker_mask.json`, the pipeline
reproduces all 130 D2 test cases exactly: status, regions, IoUs and corner errors. The
D2 splits regenerate byte-identically (`--case-set d2`).

Raw outputs are in `results/d21/{dev,test}_{baseline,masked}/`. Side-by-side tables and
before/after panels for every changed case are in
`results/d21/{dev,test}_comparison/` (`scripts/compare_runs.py`).

| Metric | Dev baseline | Dev masked | **Test baseline** | **Test masked** |
|---|---|---|---|---|
| Image-level precision | 0.917 | 0.965 | 0.882 | 0.917 |
| Image-level recall | 0.982 | 0.982 | 0.957 | 0.943 |
| Image-level F1 | 0.948 | 0.973 | 0.918 | 0.930 |
| FPR on good (perturbed) images | 0.104 | 0.042 | 0.150 (9/60) | 0.100 (6/60) |
| Region recall @ IoU ≥ 0.3 | 0.984 | 0.984 | 0.963 | 0.950 |
| Region precision @ IoU ≥ 0.3 (defect cases) | 0.679 | 0.764 | 0.630 | 0.626 |
| Marker-attributable FP regions | 16 | 0 | 32 | 2 |
| Good images with a marker-attributable region | 4 | 0 | 6 | 1 |
| Unmatched regions in defect cases (all causes) | 26 | 17 | 40 | 40 |
| Near-marker defects: image recall (n) | 1.000 (14) | 1.000 (14) | 0.960 (25) | 0.920 (25) |
| Distant defects, markers present: image recall (n) | 1.000 (23) | 1.000 (23) | 0.952 (21) | 0.952 (21) |
| Defects, no markers in scene: image recall (n) | 0.950 (20) | 0.950 (20) | 0.960 (25) | 0.960 (25) |
| Registration success (good + defect) | 1.000 | 1.000 | 1.000 | 1.000 |
| Incorrect alignments (corner error > 2 px) | 0 | 0 | 0 | 0 |
| Corner error median / max (px) | 0.064 / 0.208 | same | 0.068 / 0.172 | same |
| Registration-fail cases correctly rejected | 24/24 | 24/24 | 30/30 | 30/30 |
| Different tray: registered via ArUco, flagged / UNSAFE | 8/8, 8 / 0 | 8/8, 8 / 0 | 10/10, 10 / 0 | 10/10, 10 / 0 |
| Median time per case | 0.28 s | 0.28 s | 0.28 s | 0.28 s |

### What the test split shows

- **Marker false positives are nearly gone**: 32 → 2 regions, and good images with one
  went from 6 → 1. Four good images flipped FP → clean (`gamma_lighting_00/02/06/08`).
  Overall FPR went from 0.150 to 0.100.
- **The dev split overstated the gain.** Dev had no regressions; test has three:
  1. `subtle_foreign_02` (detected → missed). A faint object generated *touching* a
     marker's quiet zone (gap 0 px) lies inside the effective exclusion. This is the
     inherent cost of masking: **anything inside the excluded band, up to 22 px past
     a marker edge, is not inspected.** Near-marker recall drops from 0.96 to 0.92
     (1 of 25 cases).
  2. `gamma_lighting_04` (clean → 7 regions, none on markers). With gamma 0.707,
     removing marker pixels from the global tone fit moved the L gain from 1.313 to
     1.348. The linear model is wrong at that gamma either way; baseline was clean
     by luck of where the fit landed. The new regions sit on image borders and
     background.
  3. `missing_hard_03`: the same fit-shift mechanism (gain 1.268 → 1.330) adds 18
     secondary regions. The defect itself is still found (IoU 0.85).

  Because of (2) and (3), **unmatched regions in defect cases are unchanged (40 → 40)**,
  even though marker-attributable ones fell 15 → 1. Region precision is flat
  (0.630 → 0.626).
- **Different tray with the same marker ids** registers via ArUco in 10/10 cases.
  The alignment is geometrically correct for the fixture. All 10 were flagged, with a
  median of 13 regions, so none would be approved. This is safe only because the
  contents differ heavily. There is still no content-identity check, so a different
  tray with *similar* contents would be indistinguishable from a set of defects.
- **Registration is unaffected**: masking happens after registration, and the corner
  errors are identical.

### Remaining weaknesses (for D3 and beyond)

- **Global tone model.** A linear L gain/offset can't represent a strong gamma. This is
  now the main source of false positives on good images (`lighting`, `gamma_lighting`,
  `combined_nominal`) and of extra regions in hard defect cases. A fix must be
  evaluated on a new split.
- **Blind band around markers.** The agent should treat the excluded area as
  "not inspected". `report.json` exposes the polygons, so it can, for example, request
  a re-capture or a human check if a slot overlaps the band.
- **The tiny low-contrast object limit** remains. Test `subtle_foreign` misses: 3 in
  the baseline and 4 with masking; the extra one is `subtle_foreign_02`, above.
- **Bit-level float noise.** Identical `compare()` calls differ by about 1e-8 in the
  float32 anomaly map, even single-threaded; the source wasn't isolated. Thresholded
  outputs are unaffected (the full D2 regression reproduced exactly), so tests compare
  maps with a 1e-6 tolerance.
