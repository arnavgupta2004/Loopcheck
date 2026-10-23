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
