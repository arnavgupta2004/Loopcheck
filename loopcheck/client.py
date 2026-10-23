"""Minimal HTTP client for the deployed (or local) LoopCheck API. Standard library only."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

TIMEOUT_S = 35.0  # API Gateway HTTP API integrations time out at 30 s
DEFAULT_API_URL = "http://127.0.0.1:8000"  # loopcheck.local_server for development
API_URL_KEY = "LOOPCHECK_API_URL"
REDACTED_URL = "<configured API endpoint>"


def resolve_api_url(environ, secret: str | None) -> tuple[str, str]:
    """Default API base URL and where it came from: ("env" | "secrets" | "default").

    Order: the LOOPCHECK_API_URL environment variable, then the value from Streamlit secrets,
    then the local development server. Streamlit Community Cloud also exports root-level
    secrets as environment variables, so an env value equal to the secret counts as "secrets"
    (the UI hides secret-sourced endpoints). Blank values are ignored.
    """
    env = (environ.get(API_URL_KEY) or "").strip()
    sec = (secret or "").strip() if isinstance(secret, str) else ""
    if env and env != sec:
        return env, "env"
    if sec:
        return sec, "secrets"
    return DEFAULT_API_URL, "default"


def redact(message: str, url: str) -> str:
    """Remove the endpoint (with or without a trailing slash) from a user-facing message."""
    for u in sorted({url, url.rstrip("/")}, key=len, reverse=True):
        if u:
            message = message.replace(u, REDACTED_URL)
    return message


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


def smoke_test(base_url: str, reference: bytes, inspection: bytes) -> dict:
    """GET /health + POST /inspect; summarise evidence that OpenCV 5 and the real agent ran."""
    import time

    h = health(base_url)
    t0 = time.perf_counter()
    r = inspect_remote(base_url, reference, inspection)
    wall = time.perf_counter() - t0
    rep = r.get("report", {})
    return {
        "health_status": h.get("status"),
        "health_opencv_version": h.get("opencv_version"),
        "inspect_opencv_version": r.get("runtime", {}).get("opencv_version"),
        "decision": r.get("decision"),
        "termination_rule": r.get("termination_rule"),
        "error": r.get("error"),
        "agent_tool_sequence": [f"{s['rule']}:{s['tool']}" for s in rep.get("steps", [])],
        "agent_tool_calls": rep.get("tool_calls"),
        "images_returned": sorted(r.get("images", {})),
        "server_seconds": r.get("runtime", {}).get("seconds"),
        "wall_seconds": round(wall, 3),
    }


def main(argv=None) -> int:
    import argparse
    from pathlib import Path

    p = argparse.ArgumentParser(description="Smoke-test a LoopCheck API (deployed or local).")
    p.add_argument("--url", required=True, help="API base URL, e.g. https://<id>.execute-api.ap-south-1.amazonaws.com")
    p.add_argument("--case", default="data/demo_cases/02_missing_part", help="folder with reference.png + inspection.png")
    p.add_argument("--expect", help="expected decision (APPROVE/DEFECT/HUMAN_REVIEW); exit 1 if different")
    args = p.parse_args(argv)
    d = Path(args.case)
    try:
        out = smoke_test(args.url, (d / "reference.png").read_bytes(), (d / "inspection.png").read_bytes())
    except ApiError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(out, indent=2))
    ok = (out["health_status"] == "ok" and str(out["inspect_opencv_version"]).startswith("5.")
          and out["agent_tool_calls"] and not out["error"] and (args.expect is None or out["decision"] == args.expect))
    print("SMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
