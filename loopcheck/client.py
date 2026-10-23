"""Minimal HTTP client for the deployed (or local) LoopCheck API. Standard library only."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

TIMEOUT_S = 35.0  # API Gateway HTTP API integrations time out at 30 s


class ApiError(Exception):
    pass


def _request(url: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"content-type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # error bodies are JSON with decision HUMAN_REVIEW
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception as exc:
            raise ApiError(f"HTTP {e.code} with unreadable body") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(f"cannot reach {url}: {exc}") from exc


def health(base_url: str) -> dict:
    return _request(base_url.rstrip("/") + "/health")


def inspect_remote(base_url: str, reference: bytes, inspection: bytes, include_images: bool = True) -> dict:
    body = json.dumps({"reference_b64": base64.b64encode(reference).decode("ascii"),
                       "inspection_b64": base64.b64encode(inspection).decode("ascii"),
                       "options": {"include_images": include_images}}).encode("utf-8")
    return _request(base_url.rstrip("/") + "/inspect", body)
