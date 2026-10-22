"""Command-line entry point.

    uv run python -m loopcheck.cli --reference REF --inspection INSP --output-dir DIR [--config cfg.json]

Writes report.json plus overlay.png (and aligned.png, heatmap.png, anomaly_map.png when
analysis ran). Exit codes: 0 analysed, 2 input error, 3 registration failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

from .config import PipelineConfig
from .imageio import InputError, load_image
from .pipeline import STATUS_ANALYZED, STATUS_INPUT_ERROR, STATUS_REGISTRATION_FAILED, inspect
from . import OPENCV_VERSION, __version__

EXIT_CODES = {STATUS_ANALYZED: 0, STATUS_INPUT_ERROR: 2, STATUS_REGISTRATION_FAILED: 3}


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def run(reference: Path, inspection: Path, output_dir: Path, config: PipelineConfig) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        ref, ref_info = load_image(reference)
        ins, ins_info = load_image(inspection)
        result = inspect(ref, ins, config, {"reference": ref_info, "inspection": ins_info})
        report, images = result.report, result.images
    except InputError as exc:
        report = {
            "loopcheck_version": __version__,
            "opencv_version": OPENCV_VERSION,
            "status": STATUS_INPUT_ERROR,
            "error": {"code": exc.code, "message": exc.message},
            "candidate_regions": [],
        }
        images = {}
    outputs = {}
    for name, img in images.items():
        path = output_dir / f"{name}.png"
        if not cv2.imwrite(str(path), img):
            raise OSError(f"failed to write {path}")
        outputs[name] = path.name
    report["output_files"] = {**outputs, "report": "report.json"}
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, default=_json_default) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="loopcheck", description="Compare an inspection image against a reference image.")
    p.add_argument("--reference", required=True, type=Path)
    p.add_argument("--inspection", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--config", type=Path, help="JSON file overriding PipelineConfig fields")
    p.add_argument("--ecc", action="store_true", help="enable ECC refinement (off by default)")
    args = p.parse_args(argv)

    cfg = PipelineConfig.from_json(args.config) if args.config else PipelineConfig()
    if args.ecc:
        cfg.registration.ecc_enabled = True
    report = run(args.reference, args.inspection, args.output_dir, cfg)

    status = report["status"]
    line = f"status={status}"
    if status == STATUS_ANALYZED:
        reg = report["registration"]
        line += f" method={reg['method']} outcome={report['outcome']} regions={len(report['candidate_regions'])}"
    elif status == STATUS_REGISTRATION_FAILED:
        line += f" reason={report['registration']['failure_reason']}"
    else:
        line += f" error={report['error']['code']}: {report['error']['message']}"
    print(line)
    print(f"report: {args.output_dir / 'report.json'}")
    return EXIT_CODES[status]


if __name__ == "__main__":
    sys.exit(main())
