import cv2
import numpy as np
import pytest

from loopcheck import synth
from loopcheck.config import RegistrationConfig
from loopcheck.registration import (
    corner_error,
    overlap_mask,
    project_points,
    refine_ecc,
    register,
    scale_homography,
    warp_to_reference,
)

H_TRUE = np.array([[0.98, -0.05, 14.0], [0.04, 1.01, -9.0], [3e-5, -2e-5, 1.0]])


def test_opencv_major_version_is_5():
    assert cv2.__version__.startswith("5.")


def test_warp_inverse_map_brings_inspection_into_reference_frame(textured):
    h, w = textured.shape[:2]
    insp = cv2.warpPerspective(textured, H_TRUE, (w, h))  # forward: insp(H x) = ref(x)
    aligned = warp_to_reference(insp, H_TRUE, textured.shape)
    valid = overlap_mask(insp.shape, H_TRUE, textured.shape, erode_px=3) > 0
    err_inverse = np.abs(aligned.astype(int) - textured.astype(int))[valid].mean()
    # Without WARP_INVERSE_MAP the same H warps in the wrong direction.
    wrong = cv2.warpPerspective(insp, H_TRUE, (w, h))
    err_wrong = np.abs(wrong.astype(int) - textured.astype(int))[valid].mean()
    assert err_inverse < 3.0
    assert err_wrong > 5 * err_inverse


def test_project_points_follows_reference_to_inspection_convention():
    ref = np.zeros((300, 400), np.uint8)
    ref[100, 150] = 255
    insp = cv2.warpPerspective(ref, H_TRUE, (400, 300), flags=cv2.INTER_NEAREST)
    expected = project_points([[150, 100]], H_TRUE)[0]
    found = np.argwhere(insp == 255)
    assert len(found) >= 1
    y, x = found.mean(axis=0)
    assert np.hypot(x - expected[0], y - expected[1]) < 1.0


def test_find_transform_ecc_returns_reference_to_inspection_warp(textured):
    ref = cv2.GaussianBlur(cv2.cvtColor(textured, cv2.COLOR_BGR2GRAY), (0, 0), 1.5).astype(np.float32) / 255
    H_small = np.array([[1.01, 0.01, 3.0], [-0.01, 0.995, -2.0], [1e-5, 0, 1]])
    insp = cv2.warpPerspective(ref, H_small, (ref.shape[1], ref.shape[0]))
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    _, W = cv2.findTransformECC(ref, insp, np.eye(3, dtype=np.float32), cv2.MOTION_HOMOGRAPHY, crit, None, 5)
    assert corner_error(W.astype(np.float64), H_small, ref.shape) < 0.5
    aligned = warp_to_reference(insp, W.astype(np.float64), ref.shape)
    valid = overlap_mask(insp.shape, W.astype(np.float64), ref.shape, erode_px=3) > 0
    assert np.abs(aligned - ref)[valid].mean() < 0.01


def test_scale_homography_round_trip():
    H2 = scale_homography(scale_homography(H_TRUE, 0.5, 0.25), 2.0, 4.0)
    assert np.allclose(H2 / H2[2, 2], H_TRUE / H_TRUE[2, 2])
    # points scale consistently
    p = np.array([[100.0, 50.0]])
    Hs = scale_homography(H_TRUE, 0.5, 0.25)
    assert np.allclose(project_points(p * 0.5, Hs), project_points(p, H_TRUE) * 0.25)


def test_overlap_mask_excludes_unobserved_border():
    T = np.array([[1, 0, -100.0], [0, 1, 0], [0, 0, 1]])  # ref x maps to insp x - 100
    mask = overlap_mask((300, 400), T, (300, 400))
    assert mask[:, :100].max() == 0
    assert mask[:, 100:].min() == 255


def test_sift_registration_recovers_known_homography(case_factory):
    case = _case_with_markers(case_factory, "geometric", False)
    ref, ins = case["_images"]
    res = register(ref, ins, RegistrationConfig())
    assert res.ok and res.method == "sift"
    assert corner_error(res.H, np.array(case["H_true"]), ref.shape) < 1.0
    for key in ("inliers", "inlier_ratio", "reproj_error_px", "overlap_ratio"):
        assert key in res.metrics
    assert res.metrics["inlier_ratio"] >= 0.3 and res.metrics["overlap_ratio"] > 0.6


def _case_with_markers(case_factory, case_type, want=True):
    for s in range(60):
        c = case_factory(case_type, seed=s)
        if c["markers"] == want:
            return c
    pytest.fail("no suitable synthetic case")


def test_aruco_is_preferred_when_markers_visible(case_factory):
    case = _case_with_markers(case_factory, "geometric", True)
    ref, ins = case["_images"]
    res = register(ref, ins, RegistrationConfig())
    assert res.ok and res.method == "aruco"
    assert res.metrics["markers_common"] >= 3
    assert corner_error(res.H, np.array(case["H_true"]), ref.shape) < 1.0


def test_falls_back_to_sift_when_markers_hidden(case_factory):
    case = _case_with_markers(case_factory, "geometric", True)
    ref, ins = case["_images"]
    ins = ins.copy()
    # Top-left corners of markers 0 and 1 in reference px (see synth.render).
    tl = project_points([[synth.TRAY[0] + 14, synth.TRAY[1] + 14], [synth.TRAY[2] - 14 - synth.MARKER_SIZE, synth.TRAY[1] + 14]], np.array(case["H_true"]))
    for x, y in tl:  # hide two of the four markers -> only two common ids
        cv2.rectangle(ins, (int(x) - 20, int(y) - 20), (int(x) + synth.MARKER_SIZE + 20, int(y) + synth.MARKER_SIZE + 20), (128, 128, 128), -1)
    res = register(ref, ins, RegistrationConfig())
    assert res.attempts[0]["method"] == "aruco"
    assert res.attempts[0]["failure_reason"] == "too_few_common_markers"
    assert res.ok and res.method == "sift"


def test_unrelated_scene_fails_registration(case_factory):
    ref, ins = case_factory("unrelated_scene")["_images"]
    res = register(ref, ins, RegistrationConfig())
    assert not res.ok and res.H is None and res.failure_reason


def test_low_overlap_fails_registration(case_factory):
    ref, ins = case_factory("low_overlap")["_images"]
    res = register(ref, ins, RegistrationConfig())
    assert not res.ok


def test_gate_rejects_small_overlap_even_with_correct_homography(textured):
    T = np.array([[1, 0, -420.0], [0, 1, 0], [0, 0, 1]])
    insp = cv2.warpPerspective(textured, T, (640, 480))
    res = register(textured, insp, RegistrationConfig())
    assert not res.ok


def test_ecc_refinement_improves_perturbed_homography(textured):
    ref = cv2.GaussianBlur(textured, (0, 0), 1.0)
    insp = cv2.warpPerspective(ref, H_TRUE, (640, 480))
    H_init = H_TRUE.copy()
    H_init[0, 2] += 1.5
    H_init[1, 2] -= 1.0
    g = lambda x: cv2.cvtColor(x, cv2.COLOR_BGR2GRAY)  # noqa: E731
    H_ref, info = refine_ecc(g(ref), g(insp), H_init, RegistrationConfig(ecc_enabled=True, ecc_max_dim=1024))
    assert info["accepted"], info
    assert info["cc_after"] > info["cc_before"]
    assert corner_error(H_ref, H_TRUE, ref.shape) < 0.5 * corner_error(H_init, H_TRUE, ref.shape)


def test_ecc_enabled_pipeline_records_ecc_info(case_factory):
    ref, ins = case_factory("missing")["_images"]
    res = register(ref, ins, RegistrationConfig(ecc_enabled=True))
    assert res.ok and res.metrics["ecc"]["attempted"]
    assert "cc_before" in res.metrics["ecc"] or "rejected_reason" in res.metrics["ecc"]
