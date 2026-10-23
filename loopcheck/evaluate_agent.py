"""Compare the one-shot D2.1 pipeline with the D3 agent on the same synthetic cases.

    uv run --frozen python -m loopcheck.evaluate_agent --data data/synthetic/d3_dev --out results/d3/dev

Expectations (task success), defined per case category before evaluation:

  category            expected decision   also acceptable (safe)   success also requires
  nominal (good)      APPROVE             HUMAN_REVIEW (escalation)
  defect              DEFECT              HUMAN_REVIEW (escalation) >= 1 GT box matched (IoU >= 0.3)
  subtle_foreign      DEFECT or HUMAN_REVIEW (ambiguous by design)   (DEFECT must localise)
  registration_fail   HUMAN_REVIEW        -
  different_tray      HUMAN_REVIEW        DEFECT (not approved, wrong reason)

One-shot D2.1 decisions are mapped mechanically: registration failure / input error ->
HUMAN_REVIEW, analysed with >= 1 region -> DEFECT, analysed with no region -> APPROVE.
All data is SYNTHETIC.
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from . import OPENCV_VERSION
from .agent import APPROVE, DEFECT, HUMAN_REVIEW, AgentConfig, InspectionAgent
from .config import PipelineConfig
from .evaluate import iou, prf
from .imageio import load_image
from .pipeline import STATUS_ANALYZED, inspect

IOU = 0.3


def expectation(case: dict) -> dict:
    cat, t = case["category"], case["case_type"]
    if t == "subtle_foreign":
        return {"expected": [DEFECT, HUMAN_REVIEW], "safe": [], "localise": True}
    return {
        "nominal": {"expected": [APPROVE], "safe": [HUMAN_REVIEW], "localise": False},
        "defect": {"expected": [DEFECT], "safe": [HUMAN_REVIEW], "localise": True},
        "registration_fail": {"expected": [HUMAN_REVIEW], "safe": [], "localise": False},
        "different_tray": {"expected": [HUMAN_REVIEW], "safe": [DEFECT], "localise": False},
    }[cat]


def _score(case: dict, decision: str, boxes: list) -> dict:
    exp = expectation(case)
    gt = case["gt_boxes_xywh"]
    matched = [any(iou(g, b) >= IOU for b in boxes) for g in gt]
    localised = any(matched) if gt else True
    if decision in exp["expected"]:
        outcome = "success" if not (decision == DEFECT and exp["localise"] and not localised) else "detected_unlocalised"
    elif decision in exp["safe"]:
        outcome = "safe_escalation" if decision == HUMAN_REVIEW else "safe_wrong_reason"
    elif decision == APPROVE:
        outcome = "ERROR_approved"  # missed defect / approved without valid comparison
    else:
        outcome = "ERROR_false_alarm" if case["category"] == "nominal" else "ERROR_wrong_decision"
    auto = decision in (APPROVE, DEFECT)
    auto_correct = auto and ((decision == APPROVE and case["category"] == "nominal") or (decision == DEFECT and case["category"] == "defect"))
    return {"decision": decision, "outcome": outcome, "gt_matched": matched, "n_boxes": len(boxes),
            "automatic": auto, "automatic_correct": auto_correct}


def run_case(args) -> dict:
    case, data_dir, pcfg_dict, acfg_dict = args
    pcfg = PipelineConfig.from_dict(pcfg_dict) if pcfg_dict else PipelineConfig()
    acfg = AgentConfig.from_dict(acfg_dict) if acfg_dict else AgentConfig()
    ref, _ = load_image(Path(data_dir) / case["reference"])
    ins, _ = load_image(Path(data_dir) / case["inspection"])

    t = time.perf_counter()
    rep = inspect(ref, ins, pcfg).report
    one_s = time.perf_counter() - t
    if rep["status"] != STATUS_ANALYZED:
        one_dec, one_boxes = HUMAN_REVIEW, []
    else:
        one_boxes = [r["bbox_xywh_reference"] for r in rep["candidate_regions"]]
        one_dec = DEFECT if one_boxes else APPROVE

    t = time.perf_counter()
    agent_rep = InspectionAgent(pcfg, acfg).run(ref, ins)
    ag_s = time.perf_counter() - t
    ag_boxes = [d["bbox_xywh_reference"] for d in agent_rep["defects"]]

    row = {"id": case["id"], "case_type": case["case_type"], "category": case["category"], "markers": case["markers"],
           "one_shot": {**_score(case, one_dec, one_boxes), "seconds": round(one_s, 4)},
           "agent": {**_score(case, agent_rep["decision"], ag_boxes), "seconds": round(ag_s, 4),
                     "rule": agent_rep["termination_rule"], "branches": agent_rep["branches"], "tool_calls": agent_rep["tool_calls"],
                     "resolution": agent_rep["resolution_counts"], "errors": len(agent_rep["errors"]),
                     "notes": len(agent_rep.get("notes", []))}}
    return {"row": row, "trace": {"id": case["id"], **agent_rep}}


def summarize(rows: list[dict], who: str) -> dict:
    r = [x[who] | {"category": x["category"], "case_type": x["case_type"]} for x in rows]
    img = [x for x in r if x["category"] in ("nominal", "defect")]
    tp = sum(x["decision"] == DEFECT and x["category"] == "defect" for x in img)
    fp = sum(x["decision"] == DEFECT and x["category"] == "nominal" for x in img)
    fn = sum(x["decision"] != DEFECT and x["category"] == "defect" for x in img)
    nominal = [x for x in r if x["category"] == "nominal"]
    defect_rows = [x for x in r if x["category"] == "defect"]
    gt = [m for x in defect_rows for m in x["gt_matched"]]
    auto = [x for x in r if x["automatic"]]
    secs = [x["seconds"] for x in r]
    out = {
        "n": len(r),
        "image_level": {"tp": tp, "fp": fp, "fn": fn, **prf(tp, fp, fn)},
        "image_level_not_approved_as_positive": dict(zip(("precision", "recall", "f1"), prf(
            sum(x["decision"] != APPROVE and x["category"] == "defect" for x in img),
            sum(x["decision"] != APPROVE and x["category"] == "nominal" for x in img),
            sum(x["decision"] == APPROVE and x["category"] == "defect" for x in img)).values())),
        "false_positive_rate_good": fp / len(nominal) if nominal else None,
        "good_not_auto_approved_rate": sum(x["decision"] != APPROVE for x in nominal) / len(nominal) if nominal else None,
        "region_recall_iou30": sum(gt) / len(gt) if gt else None,
        "automatic_coverage": len(auto) / len(r),
        "automatic_accuracy": (sum(x["automatic_correct"] for x in auto) / len(auto)) if auto else None,
        "automatic_errors": len(auto) - sum(x["automatic_correct"] for x in auto),
        "human_review_rate": sum(x["decision"] == HUMAN_REVIEW for x in r) / len(r),
        "task_success_rate": sum(x["outcome"] == "success" for x in r) / len(r),
        "outcomes": {o: sum(x["outcome"] == o for x in r) for o in sorted({x["outcome"] for x in r})},
        "critical_errors_approved": sum(x["outcome"] == "ERROR_approved" for x in r),
        "seconds": {"mean": round(float(np.mean(secs)), 4), "median": round(float(np.median(secs)), 4), "p90": round(float(np.percentile(secs, 90)), 4)},
    }
    per_type = {}
    for t in dict.fromkeys(x["case_type"] for x in r):
        rs = [x for x in r if x["case_type"] == t]
        per_type[t] = {"n": len(rs), "success": sum(x["outcome"] == "success" for x in rs),
                       "decisions": {d: sum(x["decision"] == d for x in rs) for d in (APPROVE, DEFECT, HUMAN_REVIEW)},
                       "errors": sum(x["outcome"].startswith("ERROR") for x in rs)}
    out["per_case_type"] = per_type
    if who == "agent":
        out["branches"] = {b: sum(b in x["branches"] for x in r) for b in "ABCDEFG"}
        out["termination_rules"] = {k: sum(x["rule"] == k for x in r) for k in sorted({x["rule"] for x in r})}
        out["tool_calls"] = {"mean": round(float(np.mean([x["tool_calls"] for x in r])), 3), "max": max(x["tool_calls"] for x in r)}
        res = {k: sum(x["resolution"][k] for x in r) for k in ("confirmed", "dismissed", "ambiguous", "unresolved")}
        out["candidate_resolution"] = res
        out["cases_escalated_for_unresolved_candidates"] = sum(x["rule"] == "T2" for x in r)
        out["tool_errors"] = sum(x["errors"] for x in r)
    return out


def write_markdown(meta: dict, one: dict, ag: dict, rows: list[dict], path: Path) -> None:
    f = lambda v: "n/a" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))  # noqa: E731
    L = [f"# LoopCheck D3: one-shot D2.1 vs adaptive agent — {meta['split']} (seed {meta['seed']})", "",
         f"**SYNTHETIC DATA.** OpenCV {OPENCV_VERSION}; {one['n']} cases; same cases for both. Generated by `loopcheck.evaluate_agent`.", "",
         "| Metric | One-shot D2.1 | Agent |", "|---|---|---|"]
    rowspec = [
        ("Image-level precision (DEFECT = positive)", lambda s: s["image_level"]["precision"]),
        ("Image-level recall", lambda s: s["image_level"]["recall"]),
        ("Image-level F1", lambda s: s["image_level"]["f1"]),
        ("Recall counting HUMAN_REVIEW as not-approved", lambda s: s["image_level_not_approved_as_positive"]["recall"]),
        ("False-positive rate on good images (DEFECT)", lambda s: s["false_positive_rate_good"]),
        ("Good images not auto-approved", lambda s: s["good_not_auto_approved_rate"]),
        ("Region recall @ IoU >= 0.3 (reported defect boxes)", lambda s: s["region_recall_iou30"]),
        ("Automatic decision coverage", lambda s: s["automatic_coverage"]),
        ("Automatic decision accuracy", lambda s: s["automatic_accuracy"]),
        ("Errors among automatic decisions", lambda s: s["automatic_errors"]),
        ("HUMAN_REVIEW rate", lambda s: s["human_review_rate"]),
        ("Task success rate", lambda s: s["task_success_rate"]),
        ("Critical errors (APPROVE when not expected)", lambda s: s["critical_errors_approved"]),
        ("Latency mean / median / p90 (s)", lambda s: f"{s['seconds']['mean']} / {s['seconds']['median']} / {s['seconds']['p90']}"),
    ]
    L += [f"| {name} | {f(fn(one))} | {f(fn(ag))} |" for name, fn in rowspec]
    L += ["", "## Agent behaviour", "",
          f"- Branch activations (cases): {ag['branches']}",
          f"- Termination rules: {ag['termination_rules']}",
          f"- Tool calls per case: mean {ag['tool_calls']['mean']}, max {ag['tool_calls']['max']} (one-shot equivalent: register + compare = 2)",
          f"- Extra tool calls on cases the one-shot pipeline already decided correctly: {meta['extra_calls_on_one_shot_correct']} over {meta['n_one_shot_correct']} cases",
          f"- Candidate resolution: {ag['candidate_resolution']}",
          f"- Cases escalated because candidates stayed unresolved (T2): {ag['cases_escalated_for_unresolved_candidates']}",
          f"- Tool errors: {ag['tool_errors']}",
          f"- Outcomes one-shot: {one['outcomes']}", f"- Outcomes agent: {ag['outcomes']}", "",
          "## Per case type (success / n; decisions A/D/H)", "", "| Type | One-shot | Agent |", "|---|---|---|"]
    for t in one["per_case_type"]:
        a, b = one["per_case_type"][t], ag["per_case_type"][t]
        fmt = lambda s: f"{s['success']}/{s['n']}  ({s['decisions'][APPROVE]}/{s['decisions'][DEFECT]}/{s['decisions'][HUMAN_REVIEW]}){'  errors ' + str(s['errors']) if s['errors'] else ''}"  # noqa: E731
        L.append(f"| {t} | {fmt(a)} | {fmt(b)} |")
    changed = [x for x in rows if x["one_shot"]["outcome"] != x["agent"]["outcome"]]
    L += ["", "## Cases where the outcome differs", "", "| Case | One-shot | Agent | Agent rule | Branches |", "|---|---|---|---|---|"]
    L += [f"| {x['id']} | {x['one_shot']['decision']} ({x['one_shot']['outcome']}) | {x['agent']['decision']} ({x['agent']['outcome']}) | {x['agent']['rule']} | {''.join(x['agent']['branches'])} |" for x in changed]
    errs = [x for x in rows if x["agent"]["outcome"].startswith("ERROR")]
    L += ["", "## Agent errors", "", "| Case | Decision | Outcome | Rule |", "|---|---|---|---|"]
    L += [f"| {x['id']} | {x['agent']['decision']} | {x['agent']['outcome']} | {x['agent']['rule']} |" for x in errs] or ["| (none) | | | |"]
    path.write_text("\n".join(L) + "\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--agent-config", type=Path, help="JSON overrides for AgentConfig")
    p.add_argument("--config", type=Path, help="JSON overrides for PipelineConfig")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    manifest = json.loads((args.data / "manifest.json").read_text())
    acfg = json.loads(args.agent_config.read_text()) if args.agent_config else {}
    pcfg = json.loads(args.config.read_text()) if args.config else {}
    jobs = [(c, str(args.data), pcfg, acfg) for c in manifest["cases"]]
    with ProcessPoolExecutor(args.workers) as ex:
        results = list(ex.map(run_case, jobs, chunksize=2))
    rows = [r["row"] for r in results]
    one, ag = summarize(rows, "one_shot"), summarize(rows, "agent")
    correct = [x for x in rows if x["one_shot"]["outcome"] == "success"]
    meta = {"split": args.data.name, "seed": manifest["seed"], "opencv_version": OPENCV_VERSION,
            "n_one_shot_correct": len(correct), "extra_calls_on_one_shot_correct": sum(max(0, x["agent"]["tool_calls"] - 2) for x in correct),
            "agent_config": AgentConfig.from_dict(acfg).__dict__ if acfg else AgentConfig().__dict__}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "metrics.json").write_text(json.dumps({"meta": meta, "one_shot": one, "agent": ag}, indent=2, default=str) + "\n")
    (args.out / "per_case.json").write_text(json.dumps(rows, indent=1) + "\n")
    with gzip.open(args.out / "agent_traces.jsonl.gz", "wt") as fh:
        for r in results:
            fh.write(json.dumps(r["trace"], default=str) + "\n")
    write_markdown(meta, one, ag, rows, args.out / "summary.md")
    print(f"one-shot: F1={one['image_level']['f1']} FPR={one['false_positive_rate_good']} success={one['task_success_rate']:.3f} auto_acc={one['automatic_accuracy']}")
    print(f"agent:    F1={ag['image_level']['f1']} FPR={ag['false_positive_rate_good']} success={ag['task_success_rate']:.3f} auto_acc={ag['automatic_accuracy']} "
          f"cov={ag['automatic_coverage']:.3f} HR={ag['human_review_rate']:.3f} critical={ag['critical_errors_approved']} calls={ag['tool_calls']}")
    print(f"wrote {args.out}/summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
