"""Evaluate the deterministic pipeline on a generated synthetic split.

    uv run python -m loopcheck.evaluate --data data/synthetic/dev --out results/eval_dev

Definitions (see docs/d2_pipeline.md):
  * Image-level: positive = case has a ground-truth defect. Predicted positive = status
    "analyzed" with >= 1 candidate region. Registration failures are predicted negative
    (the system abstained) and are also reported separately.
  * FPR: fraction of nominal (defect-free) cases with >= 1 candidate region.
  * Region recall@IoU>=0.3: fraction of ground-truth boxes (defect cases) matched by any
    predicted box with IoU >= 0.3; boxes in cases that failed registration count as missed.
  * Registration: success rate on cases expected to register, corner error against the
    known H (reference corners mapped by H_est vs H_true), and correct-rejection rate on
    cases designed to fail registration.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from . import OPENCV_VERSION
from .config import PipelineConfig
from .imageio import load_image
from .pipeline import STATUS_ANALYZED, inspect
from .registration import corner_error

IOU_THRESHOLD = 0.3
NEAR_MARKER_PX = 40.0  # matches synth.NEAR_MARKER_PX
BAD_ALIGNMENT_PX = 2.0  # analysed case whose corner error exceeds this counts as misaligned
GOOD_OUTCOMES = ("tn", "tp_localized", "correct_rejection", "safe_flagged", "safe_registration_failed")


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"precision": p, "recall": r, "f1": f1}


def evaluate_case(case: dict, data_dir: Path, cfg: PipelineConfig) -> tuple[dict, dict]:
    ref, _ = load_image(data_dir / case["reference"])
    ins, _ = load_image(data_dir / case["inspection"])
    t0 = time.perf_counter()
    result = inspect(ref, ins, cfg)
    rep = result.report
    pred_boxes = [r["bbox_xywh_reference"] for r in rep.get("candidate_regions", [])]
    row = {
        "id": case["id"],
        "case_type": case["case_type"],
        "category": case["category"],
        "markers": case["markers"],
        "status": rep["status"],
        "method": rep.get("registration", {}).get("method"),
        "failure_reason": rep.get("registration", {}).get("failure_reason"),
        "n_regions": len(pred_boxes),
        "predicted_defect": rep["status"] == STATUS_ANALYZED and len(pred_boxes) > 0,
        "expected_defect": case["expected_defect"],
        "seconds": round(time.perf_counter() - t0, 3),
    }
    if case["H_true"] is not None and rep["status"] == STATUS_ANALYZED:
        H_est = np.array(rep["registration"]["homography_ref_to_insp"])
        row["corner_error_px"] = round(corner_error(H_est, np.array(case["H_true"]), ref.shape), 4)
    row["gt_ious"] = [round(max((iou(g, p) for p in pred_boxes), default=0.0), 4) for g in case["gt_boxes_xywh"]]
    row["gt_matched"] = [v >= IOU_THRESHOLD for v in row["gt_ious"]]
    row["pred_matched"] = [any(iou(g, p) >= IOU_THRESHOLD for g in case["gt_boxes_xywh"]) for p in pred_boxes]
    # D2.1 fields (manifests from D2 lack marker metadata -> treated as "no markers").
    zones = case.get("marker_zone_boxes_xywh", [])
    row["marker_fp_regions"] = sum(not m and marker_attributable(p, zones) for p, m in zip(pred_boxes, row["pred_matched"]))
    row["unmatched_regions"] = sum(not m for m in row["pred_matched"])
    gaps = case.get("gt_marker_gap_px", [None] * len(case["gt_boxes_xywh"]))
    row["gt_near_marker"] = [g is not None and g <= NEAR_MARKER_PX for g in gaps]
    row["marker_masking_applied"] = bool(rep.get("marker_masking", {}).get("applied", False))
    row["masked_fraction_of_valid"] = rep.get("marker_masking", {}).get("effective_excluded_fraction_of_valid", 0.0)
    return row, {"result": result, "ref": ref, "ins": ins, "pred_boxes": pred_boxes}


def marker_attributable(box, zones, pad: float = 20.0, min_frac: float = 0.25) -> bool:
    """A predicted box is marker-attributable if >= min_frac of it lies on a marker zone (+pad).

    The marker zone already includes the white quiet zone; pad = 20 px reaches past the
    widest mask margin evaluated (0.35 * 56 + 2 = 21.6 px from the marker edge, i.e.
    11.6 px beyond the quiet zone), so artifacts at the mask boundary are counted too.
    """
    x, y, w, h = box
    for zx, zy, zw, zh in zones:
        ix = max(0.0, min(x + w, zx + zw + pad) - max(x, zx - pad))
        iy = max(0.0, min(y + h, zy + zh + pad) - max(y, zy - pad))
        if w * h > 0 and ix * iy >= min_frac * w * h:
            return True
    return False


def summarize(rows: list[dict]) -> dict:
    img_rows = [r for r in rows if r["category"] in ("nominal", "defect")]
    tp = sum(r["predicted_defect"] and r["expected_defect"] for r in img_rows)
    fp = sum(r["predicted_defect"] and not r["expected_defect"] for r in img_rows)
    fn = sum(not r["predicted_defect"] and r["expected_defect"] for r in img_rows)
    tn = sum(not r["predicted_defect"] and not r["expected_defect"] for r in img_rows)
    nominal = [r for r in rows if r["category"] == "nominal"]
    defect = [r for r in rows if r["category"] == "defect"]
    gt_flags = [m for r in defect for m in r["gt_matched"]]
    pred_flags = [m for r in defect for m in r["pred_matched"]]
    should_register = [r for r in rows if r["category"] in ("nominal", "defect")]
    errs = [r["corner_error_px"] for r in should_register if "corner_error_px" in r]
    regfail = [r for r in rows if r["category"] == "registration_fail"]
    diff_tray = [r for r in rows if r["category"] == "different_tray"]

    def q(v, p):
        return round(float(np.percentile(v, p)), 4) if v else None

    def recall_group(pred):
        flags = [m for r in defect for m, near in zip(r["gt_matched"], r["gt_near_marker"]) if pred(r, near)]
        cases = [r for r in defect if any(pred(r, n) for n in r["gt_near_marker"])]
        return {
            "gt_boxes": len(flags),
            "region_recall_iou30": (sum(flags) / len(flags)) if flags else None,
            "cases": len(cases),
            "image_recall": (sum(r["predicted_defect"] for r in cases) / len(cases)) if cases else None,
        }

    summary = {
        "n_cases": len(rows),
        "image_level": {
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
            **prf(tp, fp, fn),
            "abstained_registration_failed": sum(r["status"] != STATUS_ANALYZED for r in img_rows),
        },
        "false_positive_rate_nominal": (sum(r["predicted_defect"] for r in nominal) / len(nominal)) if nominal else None,
        "false_regions_per_nominal_image": (sum(r["n_regions"] for r in nominal) / len(nominal)) if nominal else None,
        "region_recall_iou30": (sum(gt_flags) / len(gt_flags)) if gt_flags else None,
        "region_precision_iou30_defect_cases": (sum(pred_flags) / len(pred_flags)) if pred_flags else None,
        "registration": {
            "success_rate": sum(r["status"] == STATUS_ANALYZED for r in should_register) / len(should_register) if should_register else None,
            "corner_error_px": {"n": len(errs), "median": q(errs, 50), "p90": q(errs, 90), "max": max(errs) if errs else None},
            "method_counts": {m: sum(r["method"] == m for r in should_register if r["status"] == STATUS_ANALYZED) for m in ("aruco", "sift")},
            "correct_rejection_rate": sum(r["status"] != STATUS_ANALYZED for r in regfail) / len(regfail) if regfail else None,
        },
        "seconds_per_case": {"median": q([r["seconds"] for r in rows], 50), "max": max(r["seconds"] for r in rows)},
        "markers": {
            "marker_fp_regions_total": sum(r["marker_fp_regions"] for r in rows if r["category"] in ("nominal", "defect")),
            "marker_fp_regions_nominal": sum(r["marker_fp_regions"] for r in nominal),
            "marker_fp_regions_defect_cases": sum(r["marker_fp_regions"] for r in defect),
            "nominal_images_with_marker_fp": sum(r["marker_fp_regions"] > 0 for r in nominal),
            "unmatched_regions_defect_cases": sum(r["unmatched_regions"] for r in defect),
            "cases_with_masking_applied": sum(r["marker_masking_applied"] for r in rows),
            "median_masked_fraction_when_applied": q([r["masked_fraction_of_valid"] for r in rows if r["marker_masking_applied"]], 50),
        },
        "defects_near_vs_distant": {
            f"near_marker (gap <= {NEAR_MARKER_PX:g} px)": recall_group(lambda r, near: near),
            "distant_from_markers (markers present)": recall_group(lambda r, near: r["markers"] and not near),
            "no_markers_in_scene": recall_group(lambda r, near: not r["markers"]),
        },
        "incorrect_alignments": {
            "threshold_px": BAD_ALIGNMENT_PX,
            "count": sum(r.get("corner_error_px", 0) > BAD_ALIGNMENT_PX for r in rows),
            "cases": [r["id"] for r in rows if r.get("corner_error_px", 0) > BAD_ALIGNMENT_PX],
        },
        "different_tray": {
            "n": len(diff_tray),
            "registration_failed": sum(r["status"] != STATUS_ANALYZED for r in diff_tray),
            "registered_and_flagged": sum(r["predicted_defect"] for r in diff_tray),
            "registered_no_candidates_UNSAFE": sum(r["status"] == STATUS_ANALYZED and not r["predicted_defect"] for r in diff_tray),
            "registered_via": {m: sum(r["method"] == m for r in diff_tray) for m in ("aruco", "sift")},
            "median_regions": q([r["n_regions"] for r in diff_tray], 50),
        },
    }
    per_type = {}
    for t in dict.fromkeys(r["case_type"] for r in rows):
        rs = [r for r in rows if r["case_type"] == t]
        flags = [m for r in rs for m in r["gt_matched"]]
        e = [r["corner_error_px"] for r in rs if "corner_error_px" in r]
        per_type[t] = {
            "n": len(rs),
            "category": rs[0]["category"],
            "analyzed": sum(r["status"] == STATUS_ANALYZED for r in rs),
            "registration_failed": sum(r["status"] != STATUS_ANALYZED for r in rs),
            "flagged": sum(r["predicted_defect"] for r in rs),
            "region_recall_iou30": (sum(flags) / len(flags)) if flags else None,
            "mean_regions": round(sum(r["n_regions"] for r in rs) / len(rs), 3),
            "median_corner_error_px": q(e, 50),
        }
    summary["per_case_type"] = per_type
    return summary


def _panel(case: dict, row: dict, art: dict) -> np.ndarray:
    ref = art["ref"].copy()
    for x, y, w, h in case["gt_boxes_xywh"]:
        cv2.rectangle(ref, (x, y), (x + w, y + h), (0, 200, 0), 2)
    for x, y, w, h in art["pred_boxes"]:
        cv2.rectangle(ref, (round(x), round(y)), (round(x + w), round(y + h)), (0, 0, 255), 2)
    imgs = art["result"].images
    tiles = [ref, imgs.get("overlay", art["ins"])]
    if "heatmap" in imgs:
        tiles.append(imgs["heatmap"])
    h0 = ref.shape[0]
    tiles = [cv2.resize(t, (round(t.shape[1] * h0 / t.shape[0]), h0)) for t in tiles]
    panel = np.hstack(tiles)
    txt = f"{case['id']}  status={row['status']}  regions={row['n_regions']}  gt_iou={row['gt_ious']}  (green=GT, red=pred, ref coords)"
    cv2.rectangle(panel, (0, h0 - 28), (panel.shape[1], h0), (255, 255, 255), -1)
    cv2.putText(panel, txt, (8, h0 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)
    return cv2.resize(panel, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA)


def classify_outcome(row: dict) -> str:
    if row["category"] == "registration_fail":
        return "correct_rejection" if row["status"] != STATUS_ANALYZED else "missed_rejection"
    if row["category"] == "different_tray":
        if row["status"] != STATUS_ANALYZED:
            return "safe_registration_failed"
        return "safe_flagged" if row["predicted_defect"] else "unsafe_no_candidates"
    if row["expected_defect"]:
        if row["status"] != STATUS_ANALYZED:
            return "fn_registration_failed"
        if not row["predicted_defect"]:
            return "fn_no_regions"
        return "tp_localized" if all(row["gt_matched"]) else "tp_poorly_localized"
    if row["status"] != STATUS_ANALYZED:
        return "nominal_registration_failed"
    return "fp" if row["predicted_defect"] else "tn"


def write_markdown(summary: dict, rows: list[dict], path: Path, title: str) -> None:
    il = summary["image_level"]
    fmt = lambda v: "n/a" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))  # noqa: E731
    lines = [
        f"# {title}",
        "",
        f"**SYNTHETIC DATA.** OpenCV {OPENCV_VERSION}; {summary['n_cases']} cases. Generated by `loopcheck.evaluate`. Do not edit by hand.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Image-level precision / recall / F1 | {fmt(il['precision'])} / {fmt(il['recall'])} / {fmt(il['f1'])} |",
        f"| Confusion (TP / FP / FN / TN) | {il['confusion']['tp']} / {il['confusion']['fp']} / {il['confusion']['fn']} / {il['confusion']['tn']} |",
        f"| Nominal/defect cases that failed registration | {il['abstained_registration_failed']} |",
        f"| False-positive rate on nominal images | {fmt(summary['false_positive_rate_nominal'])} |",
        f"| False regions per nominal image | {fmt(summary['false_regions_per_nominal_image'])} |",
        f"| Region recall @ IoU>=0.3 | {fmt(summary['region_recall_iou30'])} |",
        f"| Region precision @ IoU>=0.3 (defect cases) | {fmt(summary['region_precision_iou30_defect_cases'])} |",
        f"| Registration success (should-register cases) | {fmt(summary['registration']['success_rate'])} |",
        f"| Corner error px (median / p90 / max) | {fmt(summary['registration']['corner_error_px']['median'])} / {fmt(summary['registration']['corner_error_px']['p90'])} / {fmt(summary['registration']['corner_error_px']['max'])} |",
        f"| Registration method (ArUco / SIFT) | {summary['registration']['method_counts']['aruco']} / {summary['registration']['method_counts']['sift']} |",
        f"| Correct rejection on registration-fail cases | {fmt(summary['registration']['correct_rejection_rate'])} |",
        f"| Seconds per case (median / max) | {fmt(summary['seconds_per_case']['median'])} / {fmt(summary['seconds_per_case']['max'])} |",
        "",
        "## Per case type",
        "",
        "| Type | Category | n | Analysed | Reg. failed | Flagged | Region recall@0.3 | Mean regions | Median corner err px |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t, s in summary["per_case_type"].items():
        lines.append(f"| {t} | {s['category']} | {s['n']} | {s['analyzed']} | {s['registration_failed']} | {s['flagged']} | {fmt(s['region_recall_iou30'])} | {s['mean_regions']} | {fmt(s['median_corner_error_px'])} |")
    mk, dt, ia = summary["markers"], summary["different_tray"], summary["incorrect_alignments"]
    lines += [
        "",
        "## Markers, near/distant defects, alignment",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Marker-attributable false-positive regions (nominal + defect cases) | {mk['marker_fp_regions_total']} (nominal {mk['marker_fp_regions_nominal']}, defect cases {mk['marker_fp_regions_defect_cases']}) |",
        f"| Nominal images with >= 1 marker-attributable region | {mk['nominal_images_with_marker_fp']} |",
        f"| Unmatched regions in defect cases (all causes) | {mk['unmatched_regions_defect_cases']} |",
        f"| Cases with marker masking applied / median effective excluded fraction of valid area | {mk['cases_with_masking_applied']} / {fmt(mk['median_masked_fraction_when_applied'])} |",
    ]
    for group, g in summary["defects_near_vs_distant"].items():
        lines.append(f"| Defects {group}: cases, image recall / GT boxes, region recall@0.3 | {g['cases']}, {fmt(g['image_recall'])} / {g['gt_boxes']}, {fmt(g['region_recall_iou30'])} |")
    lines += [
        f"| Incorrect alignments (corner error > {ia['threshold_px']:g} px) | {ia['count']} {ia['cases'] or ''} |",
        f"| Different tray, same marker ids (n={dt['n']}): registration failed / flagged / UNSAFE no candidates | {dt['registration_failed']} / {dt['registered_and_flagged']} / {dt['registered_no_candidates_UNSAFE']} (via ArUco {dt['registered_via']['aruco']}, SIFT {dt['registered_via']['sift']}) |",
    ]
    bad = [r for r in rows if r["outcome"] not in GOOD_OUTCOMES]
    lines += ["", "## Cases not handled correctly", "", "| Case | Outcome | Status / reason | Regions | GT IoU |", "|---|---|---|---|---|"]
    for r in bad:
        lines.append(f"| {r['id']} | {r['outcome']} | {r['status']} {r['failure_reason'] or ''} | {r['n_regions']} | {r['gt_ious']} |")
    if not bad:
        lines.append("| (none) | | | | |")
    path.write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--gallery", type=int, default=12, help="max failure panels to save (successes: one per type)")
    p.add_argument("--title", default=None, help="summary.md heading")
    args = p.parse_args(argv)

    cfg = PipelineConfig.from_json(args.config) if args.config else PipelineConfig()
    manifest = json.loads((args.data / "manifest.json").read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    gallery = args.out / "gallery"
    gallery.mkdir(exist_ok=True)

    rows, saved_success, n_fail = [], set(), 0
    for case in manifest["cases"]:
        row, art = evaluate_case(case, args.data, cfg)
        row["outcome"] = classify_outcome(row)
        rows.append(row)
        good = row["outcome"] in GOOD_OUTCOMES
        if good and case["case_type"] not in saved_success:
            saved_success.add(case["case_type"])
            cv2.imwrite(str(gallery / f"ok_{case['id']}.jpg"), _panel(case, row, art), [cv2.IMWRITE_JPEG_QUALITY, 80])
        elif not good and n_fail < args.gallery:
            n_fail += 1
            cv2.imwrite(str(gallery / f"fail_{case['id']}.jpg"), _panel(case, row, art), [cv2.IMWRITE_JPEG_QUALITY, 80])
        print(f"{row['id']:<22} {row['outcome']:<26} status={row['status']:<20} regions={row['n_regions']} iou={row['gt_ious']}")

    summary = summarize(rows)
    summary.update(opencv_version=OPENCV_VERSION, data=str(args.data), data_seed=manifest["seed"], config=cfg.to_dict())
    (args.out / "metrics.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.out / "per_case.json").write_text(json.dumps(rows, indent=1) + "\n")
    title = args.title or f"LoopCheck evaluation: {args.data.name} split (seed {manifest['seed']})"
    write_markdown(summary, rows, args.out / "summary.md", title)
    il = summary["image_level"]
    print(f"\nP={il['precision']} R={il['recall']} F1={il['f1']} FPR={summary['false_positive_rate_nominal']} "
          f"region_recall={summary['region_recall_iou30']} reg_success={summary['registration']['success_rate']}")
    print(f"wrote {args.out}/summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
