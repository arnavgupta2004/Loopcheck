"""D4: service layer, Lambda handler, local API server and client."""

import base64
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pytest

from loopcheck import aws_lambda, client, local_server, service
from loopcheck.agent import APPROVE, HUMAN_REVIEW, InspectionAgent

CASES = sorted(Path("data/demo_cases").glob("*/meta.json"))
SECRET = "DUMMY-SECRET-VALUE-0123456789"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _event(method, path, body=None, b64=False):
    return {"version": "2.0", "rawPath": path, "body": body, "isBase64Encoded": b64, "requestContext": {"http": {"method": method}}}


def _call(method, path, body=None, b64=False):
    out = aws_lambda.handler(_event(method, path, body, b64))
    return out["statusCode"], json.loads(out["body"]), out


def _png(img) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


@pytest.fixture(scope="module")
def demo_pair():
    return Path("data/demo/reference.png").read_bytes(), Path("data/demo/inspection.png").read_bytes()


@pytest.fixture(autouse=True)
def fake_secret(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)  # must never appear in any response


# --------------------------------------------------------------------------- routes
def test_health_reports_opencv5():
    code, body, out = _call("GET", "/health")
    assert code == 200 and body["status"] == "ok" and body["opencv_version"].startswith("5.")
    assert out["headers"]["content-type"] == "application/json" and SECRET not in out["body"]


@pytest.mark.parametrize("method, path, code", [("GET", "/nope", 404), ("POST", "/health", 405), ("GET", "/inspect", 405)])
def test_unknown_routes_and_methods(method, path, code):
    status, body, _ = _call(method, path)
    assert status == code and body["decision"] == HUMAN_REVIEW


# --------------------------------------------------------------------------- happy path + all decisions
def test_inspect_returns_decision_trace_and_images(demo_pair):
    status, body, out = _call("POST", "/inspect", json.dumps({"reference_b64": _b64(demo_pair[0]), "inspection_b64": _b64(demo_pair[1])}))
    assert status == 200 and body["decision"] == "DEFECT" and body["termination_rule"] == "T1"
    assert set(body["images"]) == {"overlay", "aligned", "heatmap"}
    for im in body["images"].values():
        assert cv2.imdecode(np.frombuffer(base64.b64decode(im["base64"]), np.uint8), cv2.IMREAD_COLOR) is not None
    rep = body["report"]
    assert rep["steps"] and all({"rule", "tool", "rationale", "effect"} <= set(s) for s in rep["steps"])
    assert body["runtime"]["opencv_version"].startswith("5.") and "not probabilities" in body["disclaimer"]
    assert len(out["body"]) < service.MAX_RESPONSE_BYTES and SECRET not in out["body"]


@pytest.mark.parametrize("meta_path", CASES, ids=[p.parent.name for p in CASES])
def test_bundled_demo_cases_through_handler(meta_path):
    meta = json.loads(meta_path.read_text())
    d = meta_path.parent
    body = json.dumps({"reference_b64": _b64((d / "reference.png").read_bytes()), "inspection_b64": _b64((d / "inspection.png").read_bytes()),
                       "options": {"include_images": False}})
    status, resp, _ = _call("POST", "/inspect", body)
    assert status == 200 and resp["images"] == {}
    assert resp["decision"] == meta["agent_decision_at_selection"] and resp["termination_rule"] == meta["agent_rule_at_selection"]
    assert meta["synthetic"] is True


def test_all_three_decisions_are_reachable():
    got = {json.loads(p.read_text())["agent_decision_at_selection"] for p in CASES}
    assert got == {"APPROVE", "DEFECT", "HUMAN_REVIEW"}


def test_base64_encoded_body_is_accepted(demo_pair):
    raw = json.dumps({"reference_b64": _b64(demo_pair[0]), "inspection_b64": _b64(demo_pair[1]), "options": {"include_images": False}})
    status, body, _ = _call("POST", "/inspect", _b64(raw.encode()), b64=True)
    assert status == 200 and body["decision"] == "DEFECT"


def test_data_url_prefix_is_tolerated(demo_pair):
    ref = "data:image/png;base64," + _b64(demo_pair[0])
    status, body, _ = _call("POST", "/inspect", json.dumps({"reference_b64": ref, "inspection_b64": _b64(demo_pair[1]), "options": {"include_images": False}}))
    assert status == 200


# --------------------------------------------------------------------------- malformed requests and limits
def _bad_requests(demo):
    ref = _b64(demo[0])
    big = b"\x89PNG\r\n\x1a\n" + b"\0" * (service.MAX_IMAGE_BYTES + 10)
    wide = _png(np.random.default_rng(0).integers(0, 255, (70, 4200, 3), dtype=np.uint8))
    tiny = _png(np.random.default_rng(0).integers(0, 255, (32, 32, 3), dtype=np.uint8))
    blank = _png(np.full((200, 200, 3), 128, np.uint8))
    return {
        "invalid_json": ("{not json", 400, False),
        "not_an_object": ("[1, 2]", 400, False),
        "missing_field": (json.dumps({"reference_b64": ref}), 400, False),
        "unknown_field": (json.dumps({"reference_b64": ref, "inspection_b64": ref, "evil": 1}), 400, False),
        "bad_options": (json.dumps({"reference_b64": ref, "inspection_b64": ref, "options": {"include_images": "yes"}}), 400, False),
        "invalid_base64": (json.dumps({"reference_b64": "@@@", "inspection_b64": ref}), 400, False),
        "bad_body_base64": ("%%%not-base64%%%", 400, True),
        "image_too_large": (json.dumps({"reference_b64": _b64(big), "inspection_b64": ref}), 413, False),
        "dimensions_too_large": (json.dumps({"reference_b64": _b64(wide), "inspection_b64": ref}), 413, False),
        "unsupported_format": (json.dumps({"reference_b64": _b64(b"GIF89a" + b"\0" * 100), "inspection_b64": ref}), 415, False),
        "corrupt_png": (json.dumps({"reference_b64": _b64(b"\x89PNG\r\n\x1a\n" + b"garbage" * 50), "inspection_b64": ref}), 422, False),
        "too_small": (json.dumps({"reference_b64": _b64(tiny), "inspection_b64": ref}), 422, False),
        "blank_image": (json.dumps({"reference_b64": _b64(blank), "inspection_b64": ref}), 422, False),
        "body_too_large": ("x" * (aws_lambda.MAX_BODY_BYTES * 2), 413, False),
    }


@pytest.mark.parametrize("name", list(_bad_requests((b"\x89PNG\r\n\x1a\n", b"")).keys()))
def test_malformed_requests_are_rejected_safely(name, demo_pair):
    body, code, b64 = _bad_requests(demo_pair)[name]
    status, resp, out = _call("POST", "/inspect", body, b64)
    assert status == code, resp
    assert resp["decision"] == HUMAN_REVIEW and "error" in resp and "report" not in resp
    assert SECRET not in out["body"]


# --------------------------------------------------------------------------- failures never approve
def test_agent_crash_inside_service_becomes_human_review(demo_pair, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("controller crashed")
    monkeypatch.setattr(InspectionAgent, "run", boom)
    status, body, _ = _call("POST", "/inspect", json.dumps({"reference_b64": _b64(demo_pair[0]), "inspection_b64": _b64(demo_pair[1])}))
    assert status == 200 and body["decision"] == HUMAN_REVIEW and body["termination_rule"] == "X0"


def test_unexpected_handler_error_is_500_human_review(demo_pair, monkeypatch):
    monkeypatch.setattr(aws_lambda, "analyze", lambda *a, **k: (_ for _ in ()).throw(MemoryError("oom")))
    status, body, out = _call("POST", "/inspect", json.dumps({"reference_b64": _b64(demo_pair[0]), "inspection_b64": _b64(demo_pair[1])}))
    assert status == 500 and body["decision"] == HUMAN_REVIEW and body["error"]["message"] == "MemoryError"
    assert "Traceback" not in out["body"]


def test_tool_error_never_approves_via_service(monkeypatch):
    monkeypatch.setattr(InspectionAgent, "tool_compare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    d = Path("data/demo_cases/01_clean")
    resp, _ = service.analyze((d / "reference.png").read_bytes(), (d / "inspection.png").read_bytes())
    assert resp["decision"] == HUMAN_REVIEW and resp["decision"] != APPROVE


def test_render_failure_does_not_change_decision(demo_pair, monkeypatch):
    monkeypatch.setattr(service, "render_images", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render")))
    resp, raw = service.analyze(*demo_pair)
    assert resp["decision"] == "DEFECT" and raw == {} and any(e["where"] == "render_images" for e in resp["report"]["errors"])


def test_oversized_response_drops_images_not_decision(demo_pair, monkeypatch):
    monkeypatch.setattr(service, "MAX_RESPONSE_BYTES", 50_000)
    resp, _ = service.analyze(*demo_pair)
    assert resp["images"] == {} and "images_omitted" in resp and resp["decision"] == "DEFECT"


def test_masked_area_is_dark_not_cold_in_heatmap(demo_pair):
    """Excluded marker pixels must not look like low-anomaly ("clean") pixels in the heatmap."""
    ag = InspectionAgent()
    ins = service.decode_image(demo_pair[1], "inspection")
    rep = ag.run(service.decode_image(demo_pair[0], "reference"), ins)
    heat = service.render_images(ag, ins, rep)["heatmap"]
    st = ag.last_state
    inner = cv2.erode(st.exclude, np.ones((5, 5), np.uint8)) > 0  # avoid the magenta outline
    scored = st.comparisons["default"].scoring_mask > 0
    assert inner.any() and scored.any()
    assert heat[inner].mean() < 0.5 * heat[scored].mean()  # visibly darker than any scored area


def test_trace_is_json_serialisable_roundtrip(demo_pair):
    resp, _ = service.analyze(*demo_pair, include_images=False)
    again = json.loads(service.to_json(resp))
    assert again["report"]["steps"][0]["tool"] == "register"


# --------------------------------------------------------------------------- local server + client
@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), local_server._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_local_server_and_client_end_to_end(server, demo_pair):
    assert client.health(server)["opencv_version"].startswith("5.")
    resp = client.inspect_remote(server, *demo_pair)
    assert resp["decision"] == "DEFECT" and "overlay" in resp["images"]
    bad = client.inspect_remote(server, b"not an image", demo_pair[1])
    assert bad["decision"] == HUMAN_REVIEW and bad["error"]["code"] == "unsupported_format"


def test_client_unreachable_raises():
    with pytest.raises(client.ApiError):
        client.health("http://127.0.0.1:9")
