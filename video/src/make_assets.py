"""Generate the real pipeline imagery used by the motion-graphics scenes (deterministic).

    uv run --frozen python video/src/make_assets.py

For each case it runs the REAL service (`loopcheck.service.analyze`, the same code the app
and the Lambda run) and the real registration, and saves:
  reference.png, inspection.png, aligned.png, heatmap.png, overlay.png
  case.json: decision, termination rule, tool sequence, reasons, registration method,
             homography (reference px -> inspection px), candidate boxes, ground truth.
Nothing here is drawn by hand: the video only re-frames these outputs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from loopcheck import OPENCV_VERSION  # noqa: E402
from loopcheck.service import analyze  # noqa: E402

OUT = ROOT / "video" / "assets" / "pipeline"
CASES = {"clean": "01_clean", "missing": "02_missing_part", "ambiguous": "05_ambiguous_tiny"}


def main() -> int:
    for key, folder in CASES.items():
        d = ROOT / "data" / "demo_cases" / folder
        meta = json.loads((d / "meta.json").read_text())
        ref_b, ins_b = (d / "reference.png").read_bytes(), (d / "inspection.png").read_bytes()
        resp, imgs = analyze(ref_b, ins_b, include_images=True)
        rep = resp["report"]
        out = OUT / key
        out.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out / "reference.png"), cv2.imdecode(np.frombuffer(ref_b, np.uint8), cv2.IMREAD_COLOR))
        cv2.imwrite(str(out / "inspection.png"), cv2.imdecode(np.frombuffer(ins_b, np.uint8), cv2.IMREAD_COLOR))
        for name, img in imgs.items():
            cv2.imwrite(str(out / f"{name}.png"), img)
        reg = rep.get("registration", {})
        info = {
            "case": folder, "title": meta["title"], "description": meta["description"],
            "opencv_version": OPENCV_VERSION,
            "decision": resp["decision"], "termination_rule": resp["termination_rule"], "reasons": resp["reasons"],
            "tool_sequence": [f"{s['rule']}:{s['tool']}" for s in rep.get("steps", [])],
            "registration_method": reg.get("final_method"),
            "H_ref_to_insp": reg.get("homography_ref_to_insp"),
            "candidates": [{"id": c["id"], "status": c["status"], "bbox_xywh_reference": c["bbox_xywh_reference"],
                            "peak": c["first_pass"].get("peak_score"), "area_px": c["first_pass"].get("area_px")}
                           for c in rep.get("candidates", [])],
            "coverage": rep.get("coverage"),
            "ground_truth_boxes_xywh_reference": meta["ground_truth"]["gt_boxes_xywh"],
        }
        (out / "case.json").write_text(json.dumps(info, indent=1))
        print(key, info["decision"], info["termination_rule"], info["tool_sequence"], info["registration_method"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
