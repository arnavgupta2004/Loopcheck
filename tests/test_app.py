"""D4: Streamlit UI drives the real controller (headless AppTest)."""

import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="demo group not installed (uv sync --group demo)")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py")


def _decision(at) -> str | None:
    html = " ".join(m.value for m in at.markdown)
    return next((d for d in ("APPROVE", "DEFECT", "HUMAN_REVIEW") if f">{d}<" in html), None)


@pytest.fixture
def app():
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    assert not at.exception
    return at


def test_initial_page_shows_disclaimers_and_no_decision(app):
    text = " ".join(w.value for w in app.warning)
    assert "synthetic data only" in text and "not probabilities" in text and "not inspected" in text
    assert _decision(app) is None


@pytest.mark.parametrize("slug", ["01_clean", "02_missing_part", "05_ambiguous_tiny", "08_registration_failure"])
def test_bundled_case_runs_real_controller(app, slug):
    meta = json.loads(Path(f"data/demo_cases/{slug}/meta.json").read_text())
    app.sidebar.selectbox(key="case").set_value(meta["title"])
    app.sidebar.button(key="run").click()
    app.run()
    assert not app.exception
    assert _decision(app) == meta["agent_decision_at_selection"]
    assert len(app.tabs) == 6
    assert any("Synthetic ground truth" in i.value for i in app.info)


def test_unreachable_api_is_human_review_not_approve(app):
    app.sidebar.radio(key="backend").set_value("HTTP API")
    app.run()  # the URL field is enabled only once the API backend is selected
    app.sidebar.text_input(key="api_url").set_value("http://127.0.0.1:9")
    app.sidebar.selectbox(key="case").set_value(json.loads(Path("data/demo_cases/01_clean/meta.json").read_text())["title"])
    app.sidebar.button(key="run").click()
    app.run()
    assert not app.exception
    assert _decision(app) == "HUMAN_REVIEW"
    assert any("api_unreachable" in e.value for e in app.error)


# --------------------------------------------------------------------------- API URL: env var, st.secrets, fallback
import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

from loopcheck import client  # noqa: E402
from loopcheck.local_server import _Handler  # noqa: E402

SECRET_URL_UNREACHABLE = "http://127.0.0.1:9/secret-path-xyz"


def _all_text(at) -> str:
    """Every value rendered in the app (main area + sidebar), for leak checks."""
    parts = []
    for kind in ("markdown", "caption", "error", "warning", "info", "success", "text_input", "code", "json", "title"):
        for el in getattr(at, kind, []):
            parts.append(str(getattr(el, "value", "")))
    for el in getattr(at.sidebar, "caption", []):
        parts.append(str(el.value))
    return " ".join(parts)


def _clean_env(monkeypatch):
    monkeypatch.delenv(client.API_URL_KEY, raising=False)


@pytest.mark.parametrize("env, secret, expected", [
    ({}, None, (client.DEFAULT_API_URL, "default")),
    ({}, "", (client.DEFAULT_API_URL, "default")),
    ({client.API_URL_KEY: "  "}, None, (client.DEFAULT_API_URL, "default")),
    ({client.API_URL_KEY: "http://env.example:8000"}, None, ("http://env.example:8000", "env")),
    ({}, "https://abc.execute-api.ap-south-1.amazonaws.com", ("https://abc.execute-api.ap-south-1.amazonaws.com", "secrets")),
    ({client.API_URL_KEY: "http://env.example:8000"}, "https://s.example", ("http://env.example:8000", "env")),
    # Streamlit Community Cloud also exports root-level secrets as env vars: still a secret, still hidden
    ({client.API_URL_KEY: "https://s.example"}, "https://s.example", ("https://s.example", "secrets")),
    ({}, 123, (client.DEFAULT_API_URL, "default")),
])
def test_resolve_api_url_order(env, secret, expected):
    assert client.resolve_api_url(env, secret) == expected


def test_redact_removes_endpoint_with_and_without_slash():
    msg = "cannot reach https://s.example/health: timed out (https://s.example)"
    assert "s.example" not in client.redact(msg, "https://s.example/")
    assert client.redact(msg, "https://s.example").count(client.REDACTED_URL) == 2


def test_default_url_is_localhost_without_env_or_secrets(monkeypatch):
    _clean_env(monkeypatch)  # and the repository has no .streamlit/secrets.toml: a missing file must be harmless
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    assert not at.exception
    assert at.sidebar.text_input(key="api_url").value == client.DEFAULT_API_URL


def test_env_var_sets_visible_default(monkeypatch):
    monkeypatch.setenv(client.API_URL_KEY, "http://127.0.0.1:8123")
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    assert not at.exception
    assert at.sidebar.text_input(key="api_url").value == "http://127.0.0.1:8123"


@pytest.fixture
def local_api(monkeypatch):
    """The real Lambda handler behind loopcheck.local_server on a free port."""
    monkeypatch.setattr(_Handler, "log_message", lambda *a, **k: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()
    srv.server_close()


def test_secret_url_is_used_by_inspect_remote_and_never_shown(monkeypatch, local_api):
    _clean_env(monkeypatch)
    seen = []
    real = client.inspect_remote
    monkeypatch.setattr(client, "inspect_remote", lambda url, *a, **k: (seen.append(url), real(url, *a, **k))[1])
    at = AppTest.from_file(APP, default_timeout=180)
    at.secrets[client.API_URL_KEY] = local_api
    at.run()
    assert not at.exception
    at.sidebar.radio(key="backend").set_value("HTTP API")
    at.run()
    assert not [w for w in at.sidebar.text_input if w.key == "api_url"]  # locked: no editable URL field
    assert any("configured in the app's Secrets" in c.value for c in at.sidebar.caption)
    at.sidebar.button(key="health").click()
    at.run()
    assert any("ok: OpenCV 5" in s.value for s in at.sidebar.success)
    at.sidebar.selectbox(key="case").set_value(json.loads(Path("data/demo_cases/02_missing_part/meta.json").read_text())["title"])
    at.sidebar.button(key="run").click()
    at.run()
    assert not at.exception
    assert seen == [local_api]                   # the configured endpoint, through the real HTTP client
    assert _decision(at) == "DEFECT" and "HTTP API" in " ".join(m.value for m in at.markdown)
    assert local_api.rstrip("/") not in _all_text(at)
    assert any("synthetic data only" in w.value for w in at.warning)


def test_secret_url_is_redacted_from_errors_and_fails_safe(monkeypatch):
    _clean_env(monkeypatch)
    at = AppTest.from_file(APP, default_timeout=180)
    at.secrets[client.API_URL_KEY] = SECRET_URL_UNREACHABLE
    at.run()
    at.sidebar.radio(key="backend").set_value("HTTP API")
    at.run()
    at.sidebar.button(key="health").click()
    at.run()
    at.sidebar.button(key="run").click()
    at.run()
    assert not at.exception
    assert _decision(at) == "HUMAN_REVIEW"
    errors = " ".join(e.value for e in at.error) + " ".join(e.value for e in at.sidebar.error)
    assert "api_unreachable" in errors and client.REDACTED_URL in errors
    assert "secret-path-xyz" not in _all_text(at) and "secret-path-xyz" not in errors
