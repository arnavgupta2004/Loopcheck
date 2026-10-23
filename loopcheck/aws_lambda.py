"""AWS Lambda handler for API Gateway HTTP API (payload format 2.0).

Routes:
    GET  /health   -> runtime check (OpenCV version must be 5.x)
    POST /inspect  -> {"reference_b64": str, "inspection_b64": str, "options": {"include_images": bool}}

Responses are JSON. Errors carry {"error": {...}, "decision": "HUMAN_REVIEW"}: a failed request
can never read as an approval. CORS is configured on the HTTP API (template.yaml), not here,
and CORS is not authentication.

Logging policy (tested): one structured metadata line per request (route, method, status,
decision, rule, byte counts, latency, error code / exception TYPE and code location).
Request bodies, base64 images, decoded images, exception messages, environment variables,
credentials and headers are never logged. Public responses omit internal tracebacks.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import traceback

import cv2

# Importing loopcheck enforces OpenCV 5 (ImportError otherwise -> Lambda init failure, never a
# silent OpenCV 4 run). Checked again explicitly here so the cold-start log states it.
from . import OPENCV_VERSION
from .service import MAX_IMAGE_BYTES, ServiceError, analyze, health, parse_request, to_json

if not cv2.__version__.startswith("5."):
    raise ImportError(f"OpenCV 5 required, found {cv2.__version__}")

log = logging.getLogger("loopcheck")
log.setLevel(logging.INFO)
log.info(json.dumps({"event": "cold_start", "opencv_version": OPENCV_VERSION}))

MAX_BODY_BYTES = 2 * ((MAX_IMAGE_BYTES * 4) // 3 + 8) + 4096
HEADERS = {"content-type": "application/json", "cache-control": "no-store", "x-content-type-options": "nosniff"}


def safe_frames(exc: BaseException, limit: int = 6) -> list[str]:
    """Code locations of an exception (file:line function) WITHOUT the exception message,
    source lines or locals, so request content can never reach the logs."""
    frames = traceback.extract_tb(exc.__traceback__)[-limit:]
    return [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}" for f in frames]


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": HEADERS, "body": to_json(body)}


def _log(meta: dict) -> None:
    log.info(json.dumps(meta, default=str))


def handler(event, context=None):
    t0 = time.perf_counter()
    http = (event or {}).get("requestContext", {}).get("http", {}) if isinstance(event, dict) else {}
    method, path = http.get("method", ""), (event or {}).get("rawPath", "") if isinstance(event, dict) else ""
    raw = (event.get("body") or "") if isinstance(event, dict) else ""
    meta = {"event": "request", "route": path if path in ("/health", "/inspect") else "other", "method": method,
            "body_chars": len(raw) if isinstance(raw, str) else None,
            "request_id": getattr(context, "aws_request_id", None)}
    try:
        out = _route(event, method, path, raw, meta)
    except ServiceError as exc:
        meta["error_code"] = exc.code
        out = _resp(exc.status, exc.body())
    except Exception as exc:  # never leak internals beyond the exception type; never APPROVE
        meta.update(error_type=type(exc).__name__, error_at=safe_frames(exc))
        out = _resp(500, ServiceError(500, "internal_error", f"{type(exc).__name__}").body())
    meta.update(status=out["statusCode"], response_chars=len(out["body"]), seconds=round(time.perf_counter() - t0, 4))
    _log(meta)
    return out


def _route(event, method: str, path: str, raw, meta: dict) -> dict:
    if path.endswith("/health"):
        if method != "GET":
            raise ServiceError(405, "method_not_allowed", "use GET /health")
        return _resp(200, health())
    if not path.endswith("/inspect"):
        raise ServiceError(404, "not_found", "routes: GET /health, POST /inspect")
    if method != "POST":
        raise ServiceError(405, "method_not_allowed", "use POST /inspect")
    if not isinstance(raw, str):
        raise ServiceError(400, "invalid_body", "body must be a string")
    if len(raw) > (MAX_BODY_BYTES * 4) // 3 + 8:
        raise ServiceError(413, "body_too_large", f"request body exceeds {MAX_BODY_BYTES} bytes")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception as exc:
            raise ServiceError(400, "invalid_body_encoding", f"body is not valid base64 UTF-8 JSON ({type(exc).__name__})") from exc
    if len(raw) > MAX_BODY_BYTES:
        raise ServiceError(413, "body_too_large", f"request body exceeds {MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceError(400, "invalid_json", f"body is not valid JSON at position {exc.pos}") from exc
    ref, ins, include = parse_request(body)
    meta.update(reference_bytes=len(ref), inspection_bytes=len(ins), include_images=include)
    response, _ = analyze(ref, ins, include_images=include, public=True)
    meta.update(decision=response["decision"], rule=response.get("termination_rule"),
                tool_calls=response["report"].get("tool_calls"), agent_errors=[e.get("type") for e in response["report"].get("errors", [])])
    return _resp(200, response)
