"""D3: adaptive controller. Every branch, termination path and failure mode."""

import json

import cv2
import numpy as np
import pytest

from loopcheck import agent as agent_mod
from loopcheck import cli, synth
from loopcheck.agent import (
    AMBIGUOUS, APPROVE, CONFIRMED, DEFECT, DISMISSED, HUMAN_REVIEW,
    Action, AgentConfig, AgentState, Candidate, InspectionAgent, format_trace,
)
from loopcheck.evaluate import iou


def _scene(markers: bool, seed: int = 4242) -> np.ndarray:
    c = synth.render(synth.make_scene(seed, markers))
    return c[synth.MARGIN : synth.MARGIN + synth.H, synth.MARGIN : synth.MARGIN + synth.W].copy()


def _gamma(img, g):
    return np.clip(np.power(img / 255.0, g) * 255.0, 0, 255).astype(np.uint8)


def _tools(rep):
    return [f"{s['tool']}:{s['params'].get('variant', s['params'].get('candidate', ''))}" for s in rep["steps"]]


@pytest.fixture(scope="module")
def plain():
    return _scene(False)


@pytest.fixture(scope="module")
def marked():
    return _scene(True)


@pytest.fixture(scope="module")
def agent():
    return InspectionAgent()


# --------------------------------------------------------------------------- G: clean
def test_G_clean_pair_is_approved_with_minimal_steps(agent, plain):
    rep = agent.run(plain, plain.copy())
    assert rep["decision"] == APPROVE and rep["termination_rule"] == "T5" and "G" in rep["branches"]
    assert _tools(rep) == ["register:default", "compare:default"]
    assert rep["coverage"]["inspected_fraction"] >= AgentConfig().min_coverage
    assert any("sensitivity" in lim for lim in rep["limitations"])  # an APPROVE always states what it cannot see


# --------------------------------------------------------------------------- A: registration
def test_A_registration_failure_tries_alternatives_then_escalates(agent, case_factory):
    ref, ins = case_factory("low_overlap")["_images"]
    rep = agent.run(ref, ins)
    assert _tools(rep) == ["register:default", "register:sharpness_matched_sift", "register:relaxed_ratio_sift"]
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "A2" and "A" in rep["branches"]
    assert all(not a["ok"] for a in rep["registration"]["attempts"])
    assert rep["candidates"] == [] and rep["defects"] == []


def test_A_alternative_registration_recovers_and_analysis_continues(agent, plain, monkeypatch):
    real = agent_mod.register
    calls = []

    def flaky(ref, ins, cfg):  # first (default) call fails, later calls are real
        calls.append(1)
        res = real(ref, ins, cfg)
        if len(calls) == 1:
            res.ok, res.H, res.failure_reason = False, None, "injected_failure"
        return res

    monkeypatch.setattr(agent_mod, "register", flaky)
    rep = agent.run(plain, plain.copy())
    assert _tools(rep)[:3] == ["register:default", "register:sharpness_matched_sift", "compare:default"]
    assert rep["registration"]["final_method"] is not None and rep["decision"] == APPROVE


# --------------------------------------------------------------------------- B: illumination
def test_B_strong_gamma_triggers_illumination_robust_comparison(agent, plain):
    rep = agent.run(plain, _gamma(plain, 1.4))
    assert "B" in rep["branches"] and "compare:illumination_robust" in _tools(rep)
    assert rep["observations"]["photometric"]["nonlinearity"] >= AgentConfig().nonlinearity_trigger
    assert rep["decision"] != DEFECT  # a pure tone change is never reported as a defect


def test_B_real_defect_under_gamma_is_still_found(agent, plain):
    scene = synth.make_scene(4242, False)
    scene.components = scene.components[1:]  # remove one component
    canvas = synth.render(scene)
    ins = _gamma(canvas[synth.MARGIN : synth.MARGIN + synth.H, synth.MARGIN : synth.MARGIN + synth.W], 1.4)
    gt = synth.component_bbox(synth.make_scene(4242, False).components[0])
    rep = agent.run(plain, ins)
    assert "B" in rep["branches"] and rep["decision"] == DEFECT
    assert max(iou(d["bbox_xywh_reference"], gt) for d in rep["defects"]) >= 0.3


def _cand(source="default", cue="intensity", seen_in_robust=False, area=500):
    return Candidate(0, [10, 10, 20, 20], source, {"peak_score": 0.5, "mean_score": 0.4, "area_px": area, "dominant_cue": cue,
                                                   "change_hint": "appearance_changed", "evidence": {}, "seen_in_robust": seen_in_robust})


def _views(struct_peak, others):
    v = [{"view": f"crop_local_{i}", "persist": p, "peak": 0.5 if p else 0.0} for i, p in enumerate(others)]
    return v + [{"view": "crop_structural_1x", "persist": struct_peak > 0, "peak": struct_peak}]


def _illum_state():
    st = AgentState(np.zeros((10, 10, 3), np.uint8), np.zeros((10, 10, 3), np.uint8), 1, 1)
    st.observations["photometric"] = {"gain": 1.0, "offset": 0.0, "nonlinearity": 10.0}
    return st


@pytest.mark.parametrize("cand, views, expected", [
    (_cand(), _views(0.0, [False, False, False]), DISMISSED),               # explained: photometric + illumination + absent in robust
    (_cand(seen_in_robust=True), _views(0.0, [False, False, False]), AMBIGUOUS),  # robust comparison still sees it: no dismissal
    (_cand(cue="ssim"), _views(0.0, [False, False, False]), AMBIGUOUS),     # structural cue: illumination is no explanation
    (_cand(), _views(0.55, [True, True, False]), AMBIGUOUS),               # mid structural evidence: never silently dropped
    (_cand(), _views(0.85, [True, False, False]), CONFIRMED),
    (_cand(source="sensitive"), _views(0.85, [True, True, True]), AMBIGUOUS),  # weak-mode detections escalate, never auto-DEFECT
    (_cand(source="sensitive"), _views(0.0, [False, False, False]), DISMISSED),
])
def test_B_C_D_adjudication_rules(agent, cand, views, expected):
    """Decreasing differences alone never dismiss a candidate: dismissal needs zero
    reproduction AND an explanation; anything in between stays AMBIGUOUS."""
    cand.views = views
    agent._adjudicate(_illum_state(), cand)
    assert cand.status == expected, cand.resolution_reason


def test_without_illumination_shift_unreproduced_default_candidate_is_ambiguous(agent):
    cand = _cand()
    cand.views = _views(0.0, [False, False, False])
    st = _illum_state()
    st.observations["photometric"]["nonlinearity"] = 0.5
    agent._adjudicate(st, cand)
    assert cand.status == AMBIGUOUS


# --------------------------------------------------------------------------- C: ambiguous candidate
def test_C_candidate_is_reinspected_multiscale_with_recorded_evidence(agent, case_factory):
    case = next(c for c in (case_factory("subtle_foreign", s) for s in range(12)) if not c["markers"])
    rep = agent.run(*case["_images"])
    re = [s for s in rep["steps"] if s["tool"] == "reinspect"]
    assert re, "a small first-pass candidate must be re-inspected"
    views = {v["view"] for v in re[0]["output"]["views"]}
    assert {"crop_local_1x", "crop_local_aligned_1x", "crop_structural_1x"} <= views
    assert views & {"crop_local_2x", "crop_local_0.5x"}  # a second scale
    for v in re[0]["output"]["views"]:
        assert "error" in v or {"persist", "peak", "crop_xyxy", "scale"} <= set(v)


# --------------------------------------------------------------------------- D: tiny / low contrast
def test_D_isolated_hotspot_triggers_targeted_sensitive_scan(agent, plain):
    tray = synth.make_scene(4242, False).tray_color
    ins = plain.copy()
    cv2.circle(ins, (400, 300), 6, tuple(int(max(0, v - 30)) for v in tray), -1, cv2.LINE_AA)
    rep = agent.run(plain, ins)
    assert "D" in rep["branches"] and "compare:sensitive" in _tools(rep)
    sens = [c for c in rep["candidates"] if c["source"] == "sensitive"]
    assert sens and sens[0]["status"] in (AMBIGUOUS, DISMISSED, CONFIRMED) and sens[0]["views"]
    assert rep["decision"] != APPROVE or sens[0]["status"] == DISMISSED


# --------------------------------------------------------------------------- E: marker blind spots
@pytest.mark.parametrize("offset", [3, 6, 10])
def test_E_object_inside_excluded_band_is_never_approved(agent, marked, offset):
    x, y = synth.MARKER_SPOTS[0]
    ins = marked.copy()
    cv2.circle(ins, (x + 28, y + synth.MARKER_SIZE + offset), 5, (30, 30, 200), -1, cv2.LINE_AA)
    rep = agent.run(marked, ins)
    assert "marker_band_check:" in _tools(rep) and "E" in rep["branches"]
    assert rep["decision"] != APPROVE
    assert rep["observations"]["marker_band"]["findings"]


def test_E_masked_pixels_are_reported_as_not_inspected(agent, marked):
    rep = agent.run(marked, marked.copy())
    cov = rep["coverage"]
    assert rep["excluded_polygons_reference"] and cov["excluded_markers_fraction"] > 0
    assert cov["inspected_fraction"] < cov["observed_fraction"]  # masked pixels are not counted as inspected
    assert any("NOT inspected" in r for r in rep["reasons"] + rep["limitations"])


def test_E_insufficient_coverage_escalates(marked):
    rep = InspectionAgent(agent_config=AgentConfig(min_coverage=0.95)).run(marked, marked.copy())
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "T4"


# --------------------------------------------------------------------------- F: content mismatch
def test_F_same_markers_different_tray_is_escalated(agent, case_factory):
    case = case_factory("different_tray")
    rep = agent.run(*case["_images"])
    assert rep["registration"]["final_method"] == "aruco"  # markers DO align ...
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "F2"  # ... but content does not match
    assert rep["observations"]["content"]["mismatch"]


def test_F_same_tray_passes_content_check(agent, marked):
    rep = agent.run(marked, marked.copy())
    assert "content_check:" in _tools(rep) and rep["observations"]["content"]["mismatch"] is False


# --------------------------------------------------------------------------- adaptivity
def test_different_first_pass_observations_lead_to_different_next_actions(agent, plain, marked, case_factory):
    tray = synth.make_scene(4242, False).tray_color
    tiny = plain.copy()
    cv2.circle(tiny, (400, 300), 6, tuple(int(max(0, v - 30)) for v in tray), -1, cv2.LINE_AA)
    scenarios = {
        "clean": (plain, plain.copy()),
        "gamma": (plain, _gamma(plain, 1.4)),
        "tiny": (plain, tiny),
        "markers": (marked, marked.copy()),
        "no_overlap": case_factory("low_overlap")["_images"],
    }
    seq = {name: _tools(agent.run(r, i)) for name, (r, i) in scenarios.items()}
    for name in ("clean", "gamma", "tiny", "markers"):  # identical start ...
        assert seq[name][:2] == ["register:default", "compare:default"]
    # ... and the first-pass observations alone decide what happens next.
    nxt = {name: s[2] if len(s) > 2 else "STOP" for name, s in seq.items()}
    assert seq["no_overlap"][1] == "register:sharpness_matched_sift"
    assert nxt == {"clean": "STOP", "gamma": "compare:illumination_robust", "tiny": "compare:sensitive",
                   "markers": "content_check:", "no_overlap": "register:relaxed_ratio_sift"}


# --------------------------------------------------------------------------- termination & safety
def test_step_budget_exhaustion_escalates(plain):
    rep = InspectionAgent(agent_config=AgentConfig(max_steps=1)).run(plain, plain.copy())
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "X3" and len(rep["steps"]) == 1


def test_duplicate_action_is_refused(agent, plain, monkeypatch):
    monkeypatch.setattr(InspectionAgent, "policy", lambda self, st: Action("register", {"variant": "default"}, "TEST", "loop", {}))
    rep = agent.run(plain, plain.copy())
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "X2" and len(rep["steps"]) == 1


def test_policy_exception_escalates(agent, plain, monkeypatch):
    def boom(self, st):
        raise RuntimeError("policy bug")
    monkeypatch.setattr(InspectionAgent, "policy", boom)
    rep = agent.run(plain, plain.copy())
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "X1"


@pytest.mark.parametrize("tool, scenario", [
    ("tool_register", "clean"), ("tool_compare", "clean"), ("tool_reinspect", "tiny"),
    ("tool_content_check", "markers"), ("tool_marker_band_check", "markers"),
])
def test_tool_errors_never_become_approve(plain, marked, tool, scenario, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError(f"injected failure in {tool}")
    monkeypatch.setattr(InspectionAgent, tool, boom)
    tray = synth.make_scene(4242, False).tray_color
    tiny = plain.copy()
    cv2.circle(tiny, (400, 300), 6, tuple(int(max(0, v - 30)) for v in tray), -1, cv2.LINE_AA)
    pair = {"clean": (plain, plain.copy()), "tiny": (plain, tiny), "markers": (marked, marked.copy())}[scenario]
    rep = InspectionAgent().run(*pair)
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "X4"
    assert rep["errors"] and "error" in rep["steps"][-1]


def test_failing_view_is_not_counted_as_clean(agent, case_factory, monkeypatch):
    real = InspectionAgent._crop_view

    def broken(self, st, cand, name, *a):
        if name.startswith("crop_structural"):
            raise RuntimeError("view failure")
        return real(self, st, cand, name, *a)
    monkeypatch.setattr(InspectionAgent, "_crop_view", broken)
    case = next(c for c in (case_factory("subtle_foreign", s) for s in range(12)) if not c["markers"])
    rep = agent.run(*case["_images"])
    reins = [c for c in rep["candidates"] if c["views"]]
    assert reins and all(c["status"] == AMBIGUOUS for c in reins)
    assert rep["decision"] != APPROVE


def test_input_error_is_human_review(agent):
    rep = agent.run(np.zeros((10, 10, 3), np.uint8), np.zeros((10, 10, 3), np.uint8))
    assert rep["decision"] == HUMAN_REVIEW and rep["termination_rule"] == "E0"


def test_agent_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        AgentConfig.from_dict({"no_such_threshold": 1})


# --------------------------------------------------------------------------- trace & CLI
def test_trace_records_every_step_fully(agent, case_factory):
    rep = agent.run(*case_factory("missing")["_images"])
    for i, s in enumerate(rep["steps"]):
        assert s["index"] == i
        assert {"state_before", "state_after", "rule", "tool", "params", "rationale", "observations_used", "effect", "seconds"} <= set(s)
        assert "output" in s or "error" in s
    assert rep["decision"] in (APPROVE, DEFECT, HUMAN_REVIEW) and rep["reasons"]
    text = format_trace(rep)
    assert "DECISION:" in text and all(f"[{s['index']}]" in text for s in rep["steps"])
    json.dumps(rep, default=str)


def test_cli_agent_mode_exit_codes_and_report(tmp_path, capsys):
    out = tmp_path / "o"
    code = cli.main(["--reference", "data/demo/reference.png", "--inspection", "data/demo/inspection.png", "--output-dir", str(out), "--agent"])
    rep = json.loads((out / "report.json").read_text())
    assert code == cli.AGENT_EXIT_CODES[rep["decision"]] and rep["decision"] == DEFECT and code == 4
    assert rep["steps"] and (out / "overlay.png").exists()
    assert "DECISION: DEFECT" in capsys.readouterr().out
    code = cli.main(["--reference", str(tmp_path / "nope.png"), "--inspection", str(tmp_path / "x.png"), "--output-dir", str(out), "--agent"])
    assert code == 5 and json.loads((out / "report.json").read_text())["decision"] == HUMAN_REVIEW


def test_cli_default_mode_is_unchanged(tmp_path):
    out = tmp_path / "o"
    assert cli.main(["--reference", "data/demo/reference.png", "--inspection", "data/demo/inspection.png", "--output-dir", str(out)]) == 0
    rep = json.loads((out / "report.json").read_text())
    assert rep["status"] == "analyzed" and "decision" not in rep and "mode" not in rep
