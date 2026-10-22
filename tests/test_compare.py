import cv2
import numpy as np
import pytest

from loopcheck.compare import compare, fuse, range_distance
from loopcheck.config import CompareConfig
from loopcheck.evaluate import iou


@pytest.fixture
def full_mask(textured):
    return np.full(textured.shape[:2], 255, np.uint8)


def test_identical_images_produce_no_regions(textured, full_mask):
    res = compare(textured, textured.copy(), full_mask, CompareConfig())
    assert res.regions == []
    assert res.anomaly_map.max() < 0.05


def test_pasted_block_is_localised(textured, full_mask):
    ins = textured.copy()
    cv2.rectangle(ins, (300, 200), (359, 259), (20, 200, 240), -1)
    res = compare(textured, ins, full_mask, CompareConfig())
    assert len(res.regions) >= 1
    best = max(iou(r["bbox_xywh"], [300, 200, 60, 60]) for r in res.regions)
    assert best > 0.5
    r = res.regions[0]
    assert set(r["evidence"]) >= {"intensity", "ssim", "gradient", "color"}
    assert 0 <= r["mean_score"] <= r["peak_score"] <= 1


def test_removed_object_reports_structure_removed(textured, full_mask):
    ref = textured.copy()
    cv2.circle(ref, (320, 240), 30, (250, 250, 250), -1)
    cv2.circle(ref, (320, 240), 12, (10, 10, 10), -1)
    ins = textured.copy()
    cv2.circle(ins, (320, 240), 30, (40, 40, 40), -1)  # flat fill where the part was
    res = compare(ref, ins, full_mask, CompareConfig())
    assert res.regions and res.regions[0]["change_hint"] == "structure_removed"


def test_global_brightness_and_contrast_change_is_normalised(textured, full_mask):
    ins = np.clip(textured.astype(np.float32) * 1.2 - 15, 0, 255).astype(np.uint8)
    res = compare(textured, ins, full_mask, CompareConfig())
    assert res.regions == []
    assert res.diagnostics["photometric"]["global"]["L_gain"] < 1.0


def test_one_pixel_misalignment_is_tolerated(textured, full_mask):
    ins = np.roll(textured, 1, axis=1)
    mask = full_mask.copy()
    mask[:, :3] = 0  # np.roll wraps the last column into column 0: a real change, not misalignment
    assert compare(textured, ins, mask, CompareConfig()).regions == []


def test_misalignment_tolerance_matters():
    a = np.zeros((50, 50), np.float32)
    a[:, 25:] = 200
    b = np.roll(a, 1, axis=1)  # column 0 wraps around; check the interior only
    assert range_distance(a, b, 1)[:, 2:].max() == 0
    assert range_distance(a, b, 0)[:, 2:].max() == 200


def test_tolerance_keeps_thin_defects():
    a = np.zeros((50, 50), np.float32)
    b = a.copy()
    b[:, 25] = 200  # one-pixel scratch
    assert range_distance(a, b, 1).max() == 200


def test_differences_outside_valid_mask_are_ignored(textured):
    ins = textured.copy()
    ins[:, :100] = 255
    mask = np.full(textured.shape[:2], 255, np.uint8)
    mask[:, :110] = 0
    res = compare(textured, ins, mask, CompareConfig())
    assert res.regions == []
    assert res.anomaly_map[:, :110].max() == 0


def test_sharpness_equalisation_handles_blurred_inspection(textured, full_mask):
    ins = cv2.GaussianBlur(textured, (0, 0), 1.8)
    res = compare(textured, ins, full_mask, CompareConfig())
    assert res.diagnostics["sharpness"]["blurred"] == "reference"
    assert res.diagnostics["sharpness"]["sigma"] > 0.5
    off = compare(textured, ins, full_mask, CompareConfig(equalize_sharpness=False))
    assert len(res.regions) <= len(off.regions)


def test_fuse_rejects_zero_weights(textured, full_mask):
    cues = {c: np.zeros(textured.shape[:2], np.float32) for c in ("intensity", "ssim", "gradient", "color")}
    with pytest.raises(ValueError):
        fuse(cues, full_mask, CompareConfig(weights={"intensity": 0, "ssim": 0, "gradient": 0, "color": 0}))


def test_shape_mismatch_raises(textured, full_mask):
    with pytest.raises(ValueError):
        compare(textured, textured[:100], full_mask, CompareConfig())
