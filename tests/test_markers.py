"""D2.1: ArUco marker exclusion."""

import cv2
import numpy as np
import pytest

from loopcheck import synth
from loopcheck.compare import compare
from loopcheck.config import CompareConfig, MarkerMaskConfig, PipelineConfig
from loopcheck.evaluate import iou
from loopcheck.markers import grow_quad, marker_exclusion
from loopcheck.pipeline import inspect
from loopcheck.registration import project_points

NO_MASK = PipelineConfig.from_dict({"markers": {"enabled": False}})


@pytest.fixture(scope="module")
def marker_ref():
    """Reference with all four markers, identity geometry (reference px == inspection px)."""
    scene = synth.make_scene(4242, markers=True)
    canvas = synth.render(scene)
    return canvas[synth.MARGIN : synth.MARGIN + synth.H, synth.MARGIN : synth.MARGIN + synth.W].copy()


def _gamma(img, gamma, offset):
    f = np.power(img.astype(np.float32) / 255.0, gamma) * 255.0 + offset
    return np.clip(f, 0, 255).astype(np.uint8)


def _on_marker_zone(box, pad=20):
    """Box intersects a marker + quiet-zone box grown by `pad`."""
    return any(iou(box, [z[0] - pad, z[1] - pad, z[2] + 2 * pad, z[3] + 2 * pad]) > 0 for z in synth.marker_zone_boxes())


def _blob(img, cx, cy, r=10, color=(20, 220, 240)):
    out = img.copy()
    cv2.circle(out, (cx, cy), r, color, -1, cv2.LINE_AA)
    return out


# --------------------------------------------------------------------------- geometry
def test_grow_quad_offsets_each_edge_by_exact_distance():
    sq = np.array([[0, 0], [56, 0], [56, 56], [0, 56]], float)
    assert np.allclose(grow_quad(sq, 10), [[-10, -10], [66, -10], [66, 66], [-10, 66]])
    quad = np.array([[10, 12], [70, 8], [74, 66], [6, 60]], float)  # perspective quad
    g = grow_quad(quad, 7.5)
    for i in range(4):  # distance from each grown corner pair's edge to the original edge
        a, b = quad[i], quad[(i + 1) % 4]
        n = np.array([b[1] - a[1], a[0] - b[0]]) / np.linalg.norm(b - a)
        d = abs(np.dot(g[i] - a, n)), abs(np.dot(g[(i + 1) % 4] - a, n))
        assert np.allclose(d, 7.5)


def test_mask_covers_marker_and_quiet_zone_but_stays_small(marker_ref):
    rep = inspect(marker_ref, marker_ref.copy()).report
    mm = rep["marker_masking"]
    assert mm["applied"] and {e["id"] for e in mm["markers"] if e["source"] == "reference"} == {0, 1, 2, 3}
    for e in mm["markers"]:
        poly = np.array(e["polygon_reference"], np.float32)
        z = synth.marker_zone_boxes()[e["id"]]
        for x, y in [(z[0], z[1]), (z[0] + z[2] - 1, z[1]), (z[0], z[1] + z[3] - 1), (z[0] + z[2] - 1, z[1] + z[3] - 1)]:
            assert cv2.pointPolygonTest(poly.reshape(-1, 1, 2), (float(x), float(y)), False) >= 0  # quiet zone inside
        side = np.linalg.norm(poly[1] - poly[0])
        assert side <= synth.MARKER_SIZE + 2 * (0.25 * synth.MARKER_SIZE + 2) + 1  # nothing beyond the margin
    assert mm["effective_excluded_fraction_of_valid"] < 0.10


# --------------------------------------------------------------------------- behaviour
def test_gamma_change_on_markers_no_longer_creates_regions(marker_ref):
    ins = _gamma(marker_ref, 1.25, -15)
    base = inspect(marker_ref, ins, NO_MASK).report["candidate_regions"]
    masked = inspect(marker_ref, ins).report["candidate_regions"]
    assert any(_on_marker_zone(r["bbox_xywh_reference"]) for r in base), "test premise: baseline must fire on markers"
    assert not any(_on_marker_zone(r["bbox_xywh_reference"]) for r in masked)


def test_changed_marker_interior_is_excluded_even_if_undetected_in_inspection(marker_ref):
    x, y = synth.MARKER_SPOTS[0]
    ins = marker_ref.copy()
    ins[y + 8 : y + 48, x + 8 : x + 48] = 128  # destroys marker 0 in the inspection image
    rep = inspect(marker_ref, ins).report
    assert rep["status"] == "analyzed" and rep["candidate_regions"] == []
    assert inspect(marker_ref, ins, NO_MASK).report["candidate_regions"], "test premise: baseline must fire"


def test_defect_far_from_markers_still_detected(case_factory):
    case = next(c for c in (case_factory("missing", s) for s in range(40)) if c["markers"])
    ref, ins = case["_images"]
    rep = inspect(ref, ins).report
    assert rep["marker_masking"]["applied"]
    assert max(iou(r["bbox_xywh_reference"], case["gt_boxes_xywh"][0]) for r in rep["candidate_regions"]) >= 0.3


@pytest.mark.parametrize("gap", [4, 12])
def test_defect_just_outside_effective_exclusion_is_detected(marker_ref, gap):
    # Effective exclusion edge below marker 0: marker bottom + margin (0.25*56+2) + leak 6.
    x, y = synth.MARKER_SPOTS[0]
    edge = y + synth.MARKER_SIZE + (0.25 * synth.MARKER_SIZE + 2) + 6
    r = 9
    cy = int(round(edge + gap + r))
    ins = _blob(marker_ref, x + 28, cy, r)
    rep = inspect(marker_ref, ins).report
    gt = [x + 28 - r, cy - r, 2 * r, 2 * r]
    assert max((iou(g["bbox_xywh_reference"], gt) for g in rep["candidate_regions"]), default=0) >= 0.3


def test_defect_straddling_the_mask_edge_is_detected(marker_ref):
    x, y = synth.MARKER_SPOTS[0]
    edge = int(y + synth.MARKER_SIZE + (0.25 * synth.MARKER_SIZE + 2) + 6)
    ins = _blob(marker_ref, x + 28, edge, 12)  # half inside, half outside the exclusion
    regions = inspect(marker_ref, ins).report["candidate_regions"]
    assert any(abs(g["centroid_xy"][0] - (x + 28)) < 15 and g["bbox_xywh_reference"][1] >= edge - 2 for g in regions)


def test_oversized_marker_masks_are_refused(marker_ref):
    x, y = synth.MARKER_SPOTS[0]
    ins = _blob(marker_ref, x + 28, y + synth.MARKER_SIZE + 40, 9)  # would be hidden by a huge margin
    cfg = PipelineConfig.from_dict({"markers": {"margin_frac": 3.0}})
    rep = inspect(marker_ref, ins, cfg).report
    assert all(not e["masked"] and e["reason"] == "polygon_exceeds_max_marker_area" for e in rep["marker_masking"]["markers"])
    assert rep["candidate_regions"], "defect must stay visible when the mask is refused"


def test_total_cap_disables_masking(marker_ref):
    cfg = PipelineConfig.from_dict({"markers": {"max_total_frac": 0.01}})
    rep = inspect(marker_ref, marker_ref.copy(), cfg).report
    mm = rep["marker_masking"]
    assert mm["applied"] is False and mm["skipped_reason"] == "total_masked_area_exceeds_cap"
    assert mm["effective_excluded_px"] == 0


def test_perspective_masks_from_both_images_agree(case_factory):
    case = next(c for c in (case_factory("geometric", s) for s in range(40)) if c["markers"])
    ref, ins = case["_images"]
    mm = inspect(ref, ins).report["marker_masking"]
    assert mm["common_marker_max_corner_disagreement_px"] < 1.0
    by = {(e["source"], e["id"]): np.array(e["polygon_reference"]) for e in mm["markers"]}
    for i in range(4):
        assert np.abs(by[("reference", i)] - by[("inspection", i)]).max() < 1.5


def test_marker_polygons_reported_in_original_reference_pixels(marker_ref):
    small = inspect(marker_ref, marker_ref.copy()).report["marker_masking"]["markers"]
    big_img = cv2.resize(marker_ref, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    big = inspect(big_img, big_img.copy(), PipelineConfig(work_max_dim=800)).report["marker_masking"]["markers"]
    s = {(e["source"], e["id"]): np.array(e["polygon_reference"]) for e in small}
    b = {(e["source"], e["id"]): np.array(e["polygon_reference"]) for e in big}
    for k in s:
        assert np.abs(b[k] - 2 * s[k]).max() < 3.0


def test_inspection_polygons_are_mapped_with_inverse_homography():
    H = np.array([[1.0, 0.02, 30.0], [-0.01, 1.0, -12.0], [1e-5, 0, 1.0]])
    quad = np.array([[100, 100], [156, 100], [156, 156], [100, 156]], float)
    valid = np.full((480, 640), 255, np.uint8)
    excl, info = marker_exclusion((480, 640), (480, 640), H, {}, {7: project_points(quad, H)}, valid, MarkerMaskConfig())
    poly = np.array(info["markers"][0]["polygon_reference"])
    assert np.abs(poly - grow_quad(quad, 0.25 * 56 + 2)).max() < 0.6  # grown in insp coords, mapped back
    assert excl[128, 128] == 255 and excl[300, 300] == 0


def test_registration_failure_has_no_masking_and_no_verdict(case_factory):
    ref, ins = case_factory("low_overlap")["_images"]
    rep = inspect(ref, ins).report
    assert rep["status"] == "registration_failed" and "marker_masking" not in rep and rep["candidate_regions"] == []


def test_sift_fallback_still_masks_detected_markers(case_factory):
    case = next(c for c in (case_factory("geometric", s) for s in range(40)) if c["markers"])
    ref, ins = case["_images"]
    ins = ins.copy()
    tl = project_points([synth.MARKER_SPOTS[0], synth.MARKER_SPOTS[1]], np.array(case["H_true"]))
    for x, y in tl:
        cv2.rectangle(ins, (int(x) - 20, int(y) - 20), (int(x) + 76, int(y) + 76), (128, 128, 128), -1)
    rep = inspect(ref, ins).report
    assert rep["registration"]["method"] == "sift"
    srcs = {(e["source"], e["id"]) for e in rep["marker_masking"]["markers"] if e["masked"]}
    assert {("reference", i) for i in range(4)} <= srcs and {("inspection", 2), ("inspection", 3)} <= srcs


def test_scene_without_markers_excludes_nothing(case_factory):
    case = next(c for c in (case_factory("missing", s) for s in range(40)) if not c["markers"])
    ref, ins = case["_images"]
    rep = inspect(ref, ins).report
    assert rep["marker_masking"]["markers"] == [] and rep["marker_masking"]["effective_excluded_px"] == 0
    base = inspect(ref, ins, NO_MASK).report
    assert [r["bbox_xywh_reference"] for r in rep["candidate_regions"]] == [r["bbox_xywh_reference"] for r in base["candidate_regions"]]


# --------------------------------------------------------------------------- compare-level guarantees
def test_excluded_pixels_cannot_form_regions_or_raise_neighbours(textured):
    ins = textured.copy()
    ins[200:260, 300:360] = 255 - ins[200:260, 300:360]  # huge change, fully inside the exclusion
    valid = np.full(textured.shape[:2], 255, np.uint8)
    excl = np.zeros_like(valid)
    excl[195:265, 295:365] = 255
    res = compare(textured, ins, valid, CompareConfig(), exclude=excl)
    assert res.regions == []
    ring = cv2.dilate(excl, np.ones((25, 25), np.uint8)) & ~cv2.dilate(excl, np.ones((2 * 6 + 3, 2 * 6 + 3), np.uint8))
    assert res.anomaly_map[ring > 0].max() < 0.1
    assert res.anomaly_map[excl > 0].max() == 0


def test_no_exclusion_is_identical_to_d2(textured):
    ins = cv2.GaussianBlur(textured, (0, 0), 1.2)
    cv2.circle(ins, (200, 200), 20, (0, 0, 255), -1)
    valid = np.full(textured.shape[:2], 255, np.uint8)
    a = compare(textured, ins, valid, CompareConfig())
    b = compare(textured, ins, valid, CompareConfig(), exclude=np.zeros_like(valid))
    # Repeated identical float32 OpenCV calls differ at ~1e-8 (observed even single-threaded),
    # so compare with a tolerance far below any threshold rather than bit-for-bit.
    assert np.allclose(a.anomaly_map, b.anomaly_map, atol=1e-6) and a.regions == b.regions
