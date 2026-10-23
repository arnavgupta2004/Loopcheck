"""Transport-independent inspection service (D4).

One code path for every front end: the AWS Lambda handler, the local HTTP server and the
Streamlit app (local mode) all call `analyze()`, which runs the real D3 controller.

Limits are chosen to fit AWS synchronous limits: Lambda request and response payloads are
capped at 6 MB and API Gateway HTTP API at 10 MB. Base64 adds ~33 %, so each input image is
limited to 2 MB raw (2 x 2.67 MB encoded + JSON < 6 MB) and returned images are re-encoded as
JPEG at <= 1024 px; if a response would still exceed `MAX_RESPONSE_BYTES`, images are dropped
and the response says so. Every failure yields an error object with decision HUMAN_REVIEW;
no failure path can produce APPROVE.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
import traceback

import cv2
import numpy as np

from . import OPENCV_VERSION, __version__
from .agent import HUMAN_REVIEW, AgentConfig, InspectionAgent, draw_agent_overlay
from .config import PipelineConfig
from .imageio import InputError, validate_array

SCHEMA_VERSION = "1"
MAX_IMAGE_BYTES = 2_000_000
MAX_IMAGE_SIDE = 4096
MAX_RESPONSE_BYTES = 5_500_000
RETURN_IMAGE_MAX_SIDE = 1024
RETURN_JPEG_QUALITY = 85
ALLOWED_FORMATS = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpeg"}

DISCLAIMER = ("Synthetic-data-validated research prototype. Anomaly scores are uncalibrated heuristics, not "
              "probabilities. Pixels inside excluded marker polygons were NOT inspected by the main comparison.")


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "decision": HUMAN_REVIEW,
                "error": {"code": self.code, "message": self.message}, "disclaimer": DISCLAIMER}


def sniff_format(data: bytes) -> str | None:
    return next((fmt for magic, fmt in ALLOWED_FORMATS.items() if data.startswith(magic)), None)


def decode_image(data: bytes, name: str) -> np.ndarray:
    """Bytes -> validated 8-bit BGR array; raises ServiceError (413/415/422)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ServiceError(400, "empty_image", f"{name}: no image data")
    if len(data) > MAX_IMAGE_BYTES:
        raise ServiceError(413, "image_too_large", f"{name}: {len(data)} bytes exceeds {MAX_IMAGE_BYTES}")
    if sniff_format(bytes(data[:8])) is None:
        raise ServiceError(415, "unsupported_format", f"{name}: only PNG and JPEG are accepted")
    raw = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ServiceError(422, "decode_failed", f"{name}: OpenCV could not decode the image")
    if max(raw.shape[:2]) > MAX_IMAGE_SIDE:
        raise ServiceError(413, "image_dimensions_too_large", f"{name}: {raw.shape[1]}x{raw.shape[0]} exceeds {MAX_IMAGE_SIDE}px side")
    try:
        img, _ = validate_array(raw)
    except InputError as exc:
        raise ServiceError(422, exc.code, f"{name}: {exc.message}") from exc
    return img


def decode_b64(value, name: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ServiceError(400, "missing_field", f"'{name}' must be a non-empty base64 string")
    if len(value) > (MAX_IMAGE_BYTES * 4) // 3 + 8:
        raise ServiceError(413, "image_too_large", f"{name}: encoded size exceeds the {MAX_IMAGE_BYTES}-byte image limit")
    if value.startswith("data:"):  # tolerate data URLs
        value = value.split(",", 1)[-1]
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ServiceError(400, "invalid_base64", f"{name}: not valid base64 ({exc})") from exc


def _encode_jpeg(img: np.ndarray) -> str:
    s = min(1.0, RETURN_IMAGE_MAX_SIDE / max(img.shape[:2]))
    if s < 1.0:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, RETURN_JPEG_QUALITY])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def render_images(agent: InspectionAgent, inspection: np.ndarray, report: dict) -> dict[str, np.ndarray]:
    """Visualisations of the run: annotated inspection overlay, aligned inspection and heatmap
    (both in reference coordinates, working resolution). Masked pixels are darkened, never
    shown as cold/clean."""
    images = {"overlay": draw_agent_overlay(inspection, report)}
    st = getattr(agent, "last_state", None)
    if st is not None and st.aligned is not None:
        images["aligned"] = st.aligned
        cmp = st.comparisons.get("default")
        if cmp is not None:
            heat = cv2.applyColorMap((np.clip(cmp.anomaly_map, 0, 1) * 255).astype(np.uint8), cv2.COLORMAP_JET)
            vis = cv2.addWeighted(st.aligned, 0.5, heat, 0.5, 0)
            not_scored = cmp.scoring_mask == 0
            vis[not_scored] = (vis[not_scored] * 0.25).astype(np.uint8)  # excluded / unobserved: dark, not "clean"
            if st.exclude is not None:
                cnts, _ = cv2.findContours(st.exclude, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                cv2.drawContours(vis, cnts, -1, (255, 0, 255), 1, cv2.LINE_AA)
            images["heatmap"] = vis
    return images


def analyze(reference: bytes | np.ndarray, inspection: bytes | np.ndarray, include_images: bool = True,
            pipeline_config: PipelineConfig | None = None, agent_config: AgentConfig | None = None) -> tuple[dict, dict]:
    """Run the real controller. Returns (JSON-serialisable response, raw images for local use).

    Raises ServiceError for request problems. Unexpected exceptions are converted into a
    HUMAN_REVIEW response (never APPROVE)."""
    t0 = time.perf_counter()
    ref = reference if isinstance(reference, np.ndarray) else decode_image(reference, "reference")
    ins = inspection if isinstance(inspection, np.ndarray) else decode_image(inspection, "inspection")
    agent = InspectionAgent(pipeline_config, agent_config)
    try:
        report = agent.run(ref, ins)
    except Exception as exc:  # defensive: run() already traps tool/policy errors
        report = {"decision": HUMAN_REVIEW, "termination_rule": "X0", "reasons": [f"internal error {type(exc).__name__}: {exc}"],
                  "steps": [], "errors": [{"where": "service", "error": traceback.format_exc(limit=3)}]}
    raw_images: dict[str, np.ndarray] = {}
    if include_images:
        try:
            raw_images = render_images(agent, ins, report)
        except Exception as exc:  # visualisation must never change the decision
            report.setdefault("errors", []).append({"where": "render_images", "error": f"{type(exc).__name__}: {exc}"})
    response = {
        "schema_version": SCHEMA_VERSION,
        "decision": report["decision"],
        "termination_rule": report.get("termination_rule"),
        "reasons": report.get("reasons", []),
        "disclaimer": DISCLAIMER,
        "runtime": {"opencv_version": OPENCV_VERSION, "loopcheck_version": __version__, "seconds": None},
        "report": report,
        "images": {k: {"format": "jpeg", "base64": _encode_jpeg(v)} for k, v in raw_images.items()},
    }
    response["runtime"]["seconds"] = round(time.perf_counter() - t0, 4)
    size = len(json.dumps(response, default=_json_default))
    if size > MAX_RESPONSE_BYTES:
        response["images"] = {}
        response["images_omitted"] = f"response would be {size} bytes > {MAX_RESPONSE_BYTES}"
    return response, raw_images


def parse_request(body: dict) -> tuple[bytes, bytes, bool]:
    if not isinstance(body, dict):
        raise ServiceError(400, "invalid_body", "request body must be a JSON object")
    unknown = set(body) - {"reference_b64", "inspection_b64", "options"}
    if unknown:
        raise ServiceError(400, "unknown_fields", f"unknown fields: {sorted(unknown)}")
    opts = body.get("options", {}) or {}
    if not isinstance(opts, dict) or set(opts) - {"include_images"}:
        raise ServiceError(400, "invalid_options", "options may only contain 'include_images' (bool)")
    include = opts.get("include_images", True)
    if not isinstance(include, bool):
        raise ServiceError(400, "invalid_options", "'include_images' must be a boolean")
    return decode_b64(body.get("reference_b64"), "reference_b64"), decode_b64(body.get("inspection_b64"), "inspection_b64"), include


def health() -> dict:
    return {"status": "ok" if OPENCV_VERSION.startswith("5.") else "wrong_opencv", "opencv_version": OPENCV_VERSION,
            "loopcheck_version": __version__, "schema_version": SCHEMA_VERSION,
            "limits": {"max_image_bytes": MAX_IMAGE_BYTES, "max_image_side": MAX_IMAGE_SIDE, "max_response_bytes": MAX_RESPONSE_BYTES}}


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def to_json(obj) -> str:
    return json.dumps(obj, default=_json_default)
