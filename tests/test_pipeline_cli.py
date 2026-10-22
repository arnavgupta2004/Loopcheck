import json

import cv2
import numpy as np

from loopcheck import cli, synth
from loopcheck.config import PipelineConfig
from loopcheck.evaluate import iou, prf
from loopcheck.pipeline import inspect


def _write_pair(tmp_path, case):
    ref, ins = case["_images"]
    cv2.imwrite(str(tmp_path / "ref.png"), ref)
    cv2.imwrite(str(tmp_path / "ins.png"), ins)
    return tmp_path / "ref.png", tmp_path / "ins.png"


def test_cli_missing_component_end_to_end(tmp_path, case_factory, capsys):
    case = case_factory("missing")
    ref, ins = _write_pair(tmp_path, case)
    out = tmp_path / "out"
    code = cli.main(["--reference", str(ref), "--inspection", str(ins), "--output-dir", str(out)])
    assert code == 0
    report = json.loads((out / "report.json").read_text())
    assert report["status"] == "analyzed" and report["outcome"] == "candidates_found"
    assert report["opencv_version"].startswith("5.")
    assert report["registration"]["ok"] and report["registration"]["method"] in ("aruco", "sift")
    assert len(report["registration"]["homography_ref_to_insp"]) == 3
    boxes = [r["bbox_xywh_reference"] for r in report["candidate_regions"]]
    assert max(iou(b, case["gt_boxes_xywh"][0]) for b in boxes) >= 0.3
    region = report["candidate_regions"][0]
    assert {"peak_score", "mean_score", "evidence", "polygon_inspection", "dominant_cue", "change_hint"} <= set(region)
    for name in ("overlay.png", "heatmap.png", "aligned.png", "anomaly_map.png"):
        assert cv2.imread(str(out / name)) is not None
    assert "status=analyzed" in capsys.readouterr().out


def test_cli_registration_failure_has_no_verdict(tmp_path, case_factory):
    ref, ins = _write_pair(tmp_path, case_factory("unrelated_scene"))
    out = tmp_path / "out"
    code = cli.main(["--reference", str(ref), "--inspection", str(ins), "--output-dir", str(out)])
    report = json.loads((out / "report.json").read_text())
    assert code == 3
    assert report["status"] == "registration_failed"
    assert report["candidate_regions"] == [] and "outcome" not in report
    assert report["registration"]["failure_reason"]
    assert (out / "overlay.png").exists() and not (out / "heatmap.png").exists()


def test_cli_input_error(tmp_path):
    out = tmp_path / "out"
    code = cli.main(["--reference", str(tmp_path / "missing.png"), "--inspection", str(tmp_path / "x.png"), "--output-dir", str(out)])
    report = json.loads((out / "report.json").read_text())
    assert code == 2 and report["status"] == "input_error" and report["error"]["code"] == "file_not_found"


def test_cli_config_override(tmp_path, case_factory):
    ref, ins = _write_pair(tmp_path, case_factory("identical"))
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"compare": {"threshold": 0.5}, "registration": {"min_inliers": 40}}))
    out = tmp_path / "out"
    assert cli.main(["--reference", str(ref), "--inspection", str(ins), "--output-dir", str(out), "--config", str(cfg)]) == 0
    report = json.loads((out / "report.json").read_text())
    assert report["config"]["compare"]["threshold"] == 0.5
    assert report["outcome"] == "no_candidates"


def test_identical_case_is_clean(case_factory):
    ref, ins = case_factory("identical")["_images"]
    rep = inspect(ref, ins).report
    assert rep["status"] == "analyzed" and rep["candidate_regions"] == []


def test_working_resolution_downscale_maps_boxes_back(case_factory):
    case = case_factory("foreign_object")
    ref, ins = case["_images"]
    big_ref = cv2.resize(ref, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    big_ins = cv2.resize(ins, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    rep = inspect(big_ref, big_ins, PipelineConfig(work_max_dim=800)).report
    assert rep["working_resolution"]["reference_scale"] == 0.5
    gt = [2 * v for v in case["gt_boxes_xywh"][0]]
    assert max(iou(r["bbox_xywh_reference"], gt) for r in rep["candidate_regions"]) >= 0.3


def test_inspect_rejects_bad_array():
    rep = inspect(np.zeros((10, 10, 3), np.uint8), np.zeros((10, 10, 3), np.uint8)).report
    assert rep["status"] == "input_error"


def test_synth_is_deterministic(tmp_path):
    a = synth.generate(tmp_path / "a", seed=5, per_type=1, types=["missing", "shifted"])
    b = synth.generate(tmp_path / "b", seed=5, per_type=1, types=["missing", "shifted"])
    assert a == b
    for c in a:
        for f in ("reference", "inspection"):
            assert (tmp_path / "a" / c[f]).read_bytes() == (tmp_path / "b" / c[f]).read_bytes()
    assert (tmp_path / "a" / "manifest.json").read_text() == (tmp_path / "b" / "manifest.json").read_text()


def test_synth_ground_truth_box_covers_the_change(case_factory):
    case = case_factory("foreign_object")
    ref, ins = case["_images"]
    aligned = cv2.warpPerspective(ins, np.array(case["H_true"]), (ref.shape[1], ref.shape[0]), flags=cv2.WARP_INVERSE_MAP)
    diff = cv2.absdiff(cv2.GaussianBlur(ref, (5, 5), 0), cv2.GaussianBlur(aligned, (5, 5), 0)).max(axis=2)
    x, y, w, h = case["gt_boxes_xywh"][0]
    big = diff > 60  # only the defect differs this strongly under the mild perturbation
    inside = big[max(0, y - 3) : y + h + 3, max(0, x - 3) : x + w + 3].sum()
    assert inside > 0.9 * big[20:-20, 20:-20].sum() > 0


def test_iou_and_prf():
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert iou([0, 0, 10, 10], [20, 20, 5, 5]) == 0.0
    assert abs(iou([0, 0, 10, 10], [5, 0, 10, 10]) - 50 / 150) < 1e-9
    m = prf(8, 2, 2)
    assert m["precision"] == 0.8 and m["recall"] == 0.8 and abs(m["f1"] - 0.8) < 1e-9
    assert prf(0, 0, 3)["precision"] is None
