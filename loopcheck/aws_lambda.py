"""AWS Lambda handler for API Gateway HTTP API (payload format 2.0).

Routes:
    GET  /health   -> runtime check (OpenCV version must be 5.x)
    POST /inspect  -> {"reference_b64": str, "inspection_b64": str, "options": {"include_images": bool}}

Responses are JSON. Errors carry {"error": {...}, "decision": "HUMAN_REVIEW"}: a failed request
can never read as an approval. CORS is configured on the HTTP API (template.yaml), not here.
No secrets or environment values are ever echoed back.
"""

from __future__ import annotations

import base64
import json
import logging
import traceback

import cv2

# Importing loopcheck enforces OpenCV 5 (ImportError otherwise -> Lambda init failure, never a
# silent OpenCV 4 run). Checked again explicitly here so the cold-start log states it.
from . import OPENCV_VERSION
from .service import MAX_IMAGE_BYTES, ServiceError, analyze, health, parse_request, to_json

if not cv2.__version__.startswith("5."):
    raise ImportError(f"OpenCV 5 required, found {cv2.__version__}")

log = logging.getLogger()
log.setLevel(logging.INFO)
log.info("loopcheck lambda cold start: OpenCV %s", OPENCV_VERSION)

MAX_BODY_BYTES = 2 * ((MAX_IMAGE_BYTES * 4) // 3 + 8) + 4096
HEADERS = {"content-type": "application/json", "cache-control": "no-store", "x-content-type-options": "nosniff"}


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": HEADERS, "body": to_json(body)}


def handler(event, context=None):
    try:
        http = (event or {}).get("requestContext", {}).get("http", {})
        method, path = http.get("method", ""), (event or {}).get("rawPath", "")
        if path.endswith("/health"):
            if method != "GET":
                return _resp(405, ServiceError(405, "method_not_allowed", "use GET /health").body())
            return _resp(200, health())
        if not path.endswith("/inspect"):
            return _resp(404, ServiceError(404, "not_found", "routes: GET /health, POST /inspect").body())
        if method != "POST":
            return _resp(405, ServiceError(405, "method_not_allowed", "use POST /inspect").body())

        raw = event.get("body") or ""
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
            raise ServiceError(400, "invalid_json", f"body is not valid JSON: {exc.msg}") from exc
        ref, ins, include = parse_request(body)
        response, _ = analyze(ref, ins, include_images=include)
        return _resp(200, response)
    except ServiceError as exc:
        return _resp(exc.status, exc.body())
    except Exception as exc:  # never leak internals beyond the exception type; never APPROVE
        log.error("unhandled error: %s", traceback.format_exc())
        return _resp(500, ServiceError(500, "internal_error", f"{type(exc).__name__}").body())
