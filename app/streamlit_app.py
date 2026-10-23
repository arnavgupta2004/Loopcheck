"""LoopCheck demo UI (D4).

    uv run --frozen --group demo streamlit run app/streamlit_app.py

Runs the REAL D3 controller, either in-process ("Local") or through the HTTP API
("API": the deployed AWS Lambda behind API Gateway, or `python -m loopcheck.local_server`).
API requests are made server-side by `loopcheck.client` (urllib), never from the browser.
Default API URL: the LOOPCHECK_API_URL environment variable, else LOOPCHECK_API_URL in
st.secrets (Streamlit Community Cloud "Secrets"), else http://127.0.0.1:8000. An endpoint
from secrets is never displayed: the URL field is locked and the URL is redacted from errors.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loopcheck import client  # noqa: E402
from loopcheck.service import DISCLAIMER, MAX_IMAGE_BYTES, ServiceError, analyze, to_json  # noqa: E402

CASES_DIR = ROOT / "data" / "demo_cases"
METRICS = ROOT / "results" / "d3" / "test" / "metrics.json"
COLOURS = {"APPROVE": "#1b7f3b", "DEFECT": "#c62828", "HUMAN_REVIEW": "#e07b00"}
STATUS_HELP = {"confirmed": "reproduced by independent re-inspection views", "dismissed": "not reproduced AND explained",
               "ambiguous": "conflicting or partial evidence: escalated", "unresolved": "not examined (budget/early stop): escalated"}

st.set_page_config(page_title="LoopCheck: agentic visual inspection", page_icon="🔍", layout="wide")


def _secret_api_url() -> str | None:
    """LOOPCHECK_API_URL from st.secrets; None when there is no secrets file, no key or a bad file."""
    try:
        value = st.secrets.get(client.API_URL_KEY)
    except Exception:  # FileNotFoundError / StreamlitSecretNotFoundError / parse errors: fall back silently
        return None
    return value if isinstance(value, str) else None


DEFAULT_API_URL, API_URL_SOURCE = client.resolve_api_url(os.environ, _secret_api_url())
HIDE_API_URL = API_URL_SOURCE == "secrets"


def _safe(message: str, url: str) -> str:
    return client.redact(message, url) if HIDE_API_URL else message


# --------------------------------------------------------------------------- helpers
@st.cache_data
def list_cases() -> dict[str, dict]:
    cases = {}
    for d in sorted(CASES_DIR.glob("*/meta.json")):
        meta = json.loads(d.read_text())
        meta["dir"] = str(d.parent)
        cases[meta["title"]] = meta
    return cases


def to_rgb(img_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)


def decode_bytes(data: bytes) -> np.ndarray | None:
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return arr


def b64_to_rgb(b64: str) -> np.ndarray:
    return to_rgb(decode_bytes(base64.b64decode(b64)))


def run_inspection(backend: str, api_url: str, ref: bytes, ins: bytes) -> dict:
    """Returns {"response": dict, "images": {name: rgb}, "backend": str, "wall_s": float} or {"error": ...}."""
    t0 = time.perf_counter()
    try:
        if backend == "Local (in-process)":
            response, raw = analyze(ref, ins, include_images=True)
            images = {k: to_rgb(v) for k, v in raw.items()}
        else:
            response = client.inspect_remote(api_url, ref, ins)
            images = {k: b64_to_rgb(v["base64"]) for k, v in response.get("images", {}).items()}
        return {"response": response, "images": images, "backend": backend, "wall_s": time.perf_counter() - t0}
    except ServiceError as exc:
        return {"response": exc.body(), "images": {}, "backend": backend, "wall_s": time.perf_counter() - t0}
    except client.ApiError as exc:
        return {"response": {"decision": "HUMAN_REVIEW", "error": {"code": "api_unreachable", "message": _safe(str(exc), api_url)}},
                "images": {}, "backend": backend, "wall_s": time.perf_counter() - t0}
    except Exception as exc:  # the UI never turns a failure into APPROVE
        return {"response": {"decision": "HUMAN_REVIEW", "error": {"code": "ui_exception", "message": f"{type(exc).__name__}: {exc}"}},
                "images": {}, "backend": backend, "wall_s": time.perf_counter() - t0}


def draw_gt(ref_bgr: np.ndarray, boxes) -> np.ndarray:
    out = ref_bgr.copy()
    for x, y, w, h in boxes:
        cv2.rectangle(out, (int(x), int(y)), (int(x + w), int(y + h)), (0, 200, 0), 2)
    return to_rgb(out)


# --------------------------------------------------------------------------- header
st.title("LoopCheck: agentic visual inspection")
st.caption("Reference-vs-inspection comparison with OpenCV 5 and a transparent perception–decision–action controller.")
st.warning(
    "**Research prototype, validated on synthetic data only.** Metrics and bundled cases come from a synthetic tray "
    "generator and do not establish real-world performance. Anomaly scores are uncalibrated heuristics, **not probabilities**. "
    "Areas inside excluded ArUco-marker polygons are **not inspected** by the main comparison and are never reported as clean.",
    icon="⚠️",
)

# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Backend")
    backend = st.radio("Run the controller", ["Local (in-process)", "HTTP API"], key="backend",
                       help="Local runs loopcheck.service in this process. HTTP API calls the deployed Lambda (or loopcheck.local_server).")
    if HIDE_API_URL:  # configured in Secrets: use it, never show it, and do not let visitors point the server elsewhere
        api_url = DEFAULT_API_URL
        st.caption("API endpoint: configured in the app's Secrets (not shown).")
    else:
        api_url = st.text_input("API base URL", DEFAULT_API_URL, key="api_url", disabled=backend != "HTTP API")
    if backend == "HTTP API" and st.button("Check API health", key="health"):
        try:
            h = client.health(api_url)
            (st.success if h.get("status") == "ok" else st.error)(f"{h.get('status')}: OpenCV {h.get('opencv_version')}")
        except client.ApiError as exc:
            st.error(_safe(str(exc), api_url))

    st.header("Input")
    source = st.radio("Images", ["Bundled synthetic case", "Upload"], key="source")
    cases = list_cases()
    ref_bytes = ins_bytes = None
    meta = None
    if source == "Bundled synthetic case":
        title = st.selectbox("Case", list(cases), key="case")
        meta = cases[title]
        st.caption(f"SYNTHETIC · {meta['description']}")
        ref_bytes = (Path(meta["dir"]) / "reference.png").read_bytes()
        ins_bytes = (Path(meta["dir"]) / "inspection.png").read_bytes()
    else:
        st.caption(f"PNG or JPEG, ≤ {MAX_IMAGE_BYTES / 1e6:.0f} MB and ≤ 4096 px per side each.")
        up_ref = st.file_uploader("Reference image", type=["png", "jpg", "jpeg"], key="up_ref")
        up_ins = st.file_uploader("Inspection image", type=["png", "jpg", "jpeg"], key="up_ins")
        ref_bytes = up_ref.getvalue() if up_ref else None
        ins_bytes = up_ins.getvalue() if up_ins else None
    run = st.button("Run inspection agent", type="primary", key="run", disabled=not (ref_bytes and ins_bytes), width="stretch")

if run and ref_bytes and ins_bytes:
    with st.spinner("Running the agent controller…"):
        st.session_state["result"] = run_inspection(backend, api_url, ref_bytes, ins_bytes)
        st.session_state["inputs"] = (ref_bytes, ins_bytes, meta)

if "result" not in st.session_state:
    st.info("Choose a bundled synthetic case or upload two images, then press **Run inspection agent**.")
    if METRICS.exists():
        with st.expander("Aggregate held-out evaluation (synthetic, D3 test split)"):
            m = json.loads(METRICS.read_text())
            a, o = m["agent"], m["one_shot"]
            st.table({"metric": ["errors among automatic decisions", "critical errors (approved when not expected)", "false-positive rate on good images",
                                 "HUMAN_REVIEW rate", "automatic coverage", "task success"],
                      "one-shot D2.1": [o["automatic_errors"], o["critical_errors_approved"], round(o["false_positive_rate_good"], 3),
                                        round(o["human_review_rate"], 3), round(o["automatic_coverage"], 3), round(o["task_success_rate"], 3)],
                      "agent (D3)": [a["automatic_errors"], a["critical_errors_approved"], round(a["false_positive_rate_good"], 3),
                                     round(a["human_review_rate"], 3), round(a["automatic_coverage"], 3), round(a["task_success_rate"], 3)]})
            st.caption(f"{m['agent']['n']} synthetic cases, evaluated once after freezing thresholds. Synthetic results do not establish real-world performance.")
    st.stop()

# --------------------------------------------------------------------------- results
res = st.session_state["result"]
resp = res["response"]
ref_bytes, ins_bytes, meta = st.session_state["inputs"]
decision = resp.get("decision", "HUMAN_REVIEW")
report = resp.get("report", {})

st.markdown(
    f"<div style='padding:14px 18px;border-radius:8px;background:{COLOURS.get(decision, '#555')};color:white'>"
    f"<span style='font-size:1.6rem;font-weight:700'>{decision}</span>"
    f"<span style='margin-left:14px;opacity:.9'>rule {resp.get('termination_rule') or report.get('termination_rule') or '—'}"
    f" · {res['backend']} · {res['wall_s']:.2f}s wall</span></div>", unsafe_allow_html=True)

if "error" in resp:
    st.error(f"**{resp['error']['code']}**: {resp['error']['message']}. The request was not analysed; the safe outcome is HUMAN_REVIEW.")
for r in resp.get("reasons", []):
    st.markdown(f"- {r}")
if resp.get("images_omitted"):
    st.warning(f"Images omitted from the API response: {resp['images_omitted']}")

if meta:
    gt = meta["ground_truth"]
    st.info(f"**Synthetic ground truth** · category `{meta['category']}` · defect `{gt['defect_type']}` · "
            f"{len(gt['gt_boxes_xywh'])} box(es) (green in the reference view). {meta['selection']}.")

tabs = st.tabs(["Images", "Detected regions", "Agent trace", "Registration & coverage", "Limitations", "Raw JSON & downloads"])

with tabs[0]:
    ref_img, ins_img = decode_bytes(ref_bytes), decode_bytes(ins_bytes)
    c1, c2 = st.columns(2)
    c1.image(draw_gt(ref_img, meta["ground_truth"]["gt_boxes_xywh"]) if meta and ref_img is not None else (to_rgb(ref_img) if ref_img is not None else None),
             caption="Reference" + (" (green = synthetic ground truth)" if meta else ""), width="stretch")
    if ins_img is not None:
        c2.image(to_rgb(ins_img), caption="Inspection (input)", width="stretch")
    imgs = res["images"]
    if imgs:
        c3, c4, c5 = st.columns(3)
        if "overlay" in imgs:
            c3.image(imgs["overlay"], caption="Annotated result on the inspection image (red confirmed, orange ambiguous, grey dismissed, magenta = excluded, NOT inspected)", width="stretch")
        if "aligned" in imgs:
            c4.image(imgs["aligned"], caption="Inspection aligned to the reference frame", width="stretch")
        if "heatmap" in imgs:
            c5.image(imgs["heatmap"], caption="Default-pass anomaly heatmap (uncalibrated). Dark = not scored (outside overlap or excluded marker band, magenta outline): NOT clean", width="stretch")
    else:
        st.caption("No result images (the request failed or registration did not succeed).")

with tabs[1]:
    cands = report.get("candidates", [])
    if not cands:
        st.write("No candidate regions.")
    else:
        st.caption("Scores are uncalibrated heuristics in [0, 1], not probabilities. Boxes are in reference pixels [x, y, w, h].")
        st.dataframe([{"id": c["id"], "status": c["status"], "source": c["source"], "box (ref px)": c["bbox_xywh_reference"],
                       "first-pass peak": c["first_pass"].get("peak_score"), "mean": c["first_pass"].get("mean_score"),
                       "area px": c["first_pass"].get("area_px"), "dominant cue": c["first_pass"].get("dominant_cue"),
                       "resolution": c["reason"]} for c in cands], width="stretch", hide_index=True)
        for c in cands:
            if c.get("views"):
                with st.expander(f"Candidate {c['id']} — {c['status']} ({STATUS_HELP.get(c['status'], '')}): re-inspection evidence"):
                    st.dataframe([{k: v.get(k) for k in ("view", "scale", "persist", "peak", "mean", "local_shift_px", "error")} for v in c["views"]],
                                 width="stretch", hide_index=True)

with tabs[2]:
    steps, notes = report.get("steps", []), report.get("notes", [])
    st.caption("Chronological trace: each step shows the policy rule that fired, the observations it used, the tool and parameters, "
               "the output and how state changed.")
    for s in steps:
        params = ", ".join(f"{k}={v}" for k, v in s["params"].items())
        with st.expander(f"Step {s['index']} · rule {s['rule']} · {s['tool']}({params}) · {s['seconds']:.2f}s", expanded=s["index"] < 2):
            st.markdown(f"**Why:** {s['rationale']}")
            if s.get("observations_used"):
                st.markdown("**Observations used:**")
                st.json(s["observations_used"], expanded=False)
            if "error" in s:
                st.error(f"Tool error: {s['error']} → decision forced to HUMAN_REVIEW")
            else:
                st.markdown("**Output:**")
                st.json(s.get("output", {}), expanded=False)
            st.markdown(f"**Effect on state:** {s['effect']}")
        for n in notes:
            if n["after_step"] == s["index"]:
                st.markdown(f"↳ *policy note ({n['rule']}): {n['note']}*")
    if steps or "error" in resp:
        st.markdown(f"**Final decision:** `{decision}` (rule `{resp.get('termination_rule') or report.get('termination_rule')}`)")

with tabs[3]:
    reg = report.get("registration", {})
    cov = report.get("coverage") or {}
    a, b, c, d = st.columns(4)
    a.metric("Registration", reg.get("final_method") or "failed")
    b.metric("Observed (overlap)", f"{cov.get('observed_fraction', 0):.1%}" if cov else "—")
    c.metric("Inspected", f"{cov.get('inspected_fraction', 0):.1%}" if cov else "—")
    d.metric("Excluded markers (NOT inspected)", f"{cov.get('excluded_markers_fraction', 0):.1%}" if cov else "—")
    if reg.get("attempts"):
        st.markdown("**Registration attempts**")
        st.dataframe([{"variant": x["variant"], "ok": x["ok"], "method": x["method"], "failure": x["failure_reason"],
                       "inliers": x["metrics"].get("inliers"), "inlier ratio": x["metrics"].get("inlier_ratio"),
                       "reproj. error px": x["metrics"].get("reproj_error_px"), "overlap": x["metrics"].get("overlap_ratio"),
                       "common markers": x["metrics"].get("markers_common")} for x in reg["attempts"]], width="stretch", hide_index=True)
    if reg.get("homography_ref_to_insp"):
        st.markdown("**Homography (reference px → inspection px)**")
        st.code(np.array2string(np.array(reg["homography_ref_to_insp"]), precision=5, suppress_small=True))
    if report.get("excluded_polygons_reference"):
        st.markdown("**Excluded marker polygons (reference px) — NOT inspected by the main comparison; screened by the unmasked band diagnostic**")
        st.dataframe([{"marker id": p["id"], "detected in": p["source"], "polygon": p["polygon_reference"]} for p in report["excluded_polygons_reference"]],
                     width="stretch", hide_index=True)
    obs = report.get("observations", {})
    if obs:
        st.markdown("**Observations**")
        st.json({k: obs[k] for k in ("photometric", "sharpness", "widespread", "hotspots", "content", "marker_band") if k in obs}, expanded=False)
    if steps := report.get("steps"):
        st.markdown(f"**Latency** — controller {report.get('seconds', 0):.2f}s, service {resp.get('runtime', {}).get('seconds') or 0:.2f}s, wall {res['wall_s']:.2f}s")
        st.bar_chart({f"{s['index']} {s['tool']}": s["seconds"] for s in steps})

with tabs[4]:
    for lim in report.get("limitations", []):
        st.markdown(f"- {lim}")
    st.markdown(f"- {DISCLAIMER}")
    st.markdown("- Thresholds were tuned on a synthetic development split; no real-photograph evaluation exists yet.")
    st.markdown("- HUMAN_REVIEW is a deliberate outcome for uncertain alignment, conflicting evidence, unscreened blind spots or errors.")

with tabs[5]:
    payload = {k: v for k, v in resp.items() if k != "images"}
    st.download_button("Download JSON report", to_json(payload).encode("utf-8"), file_name="loopcheck_report.json", mime="application/json", key="dl_json")
    if "overlay" in res["images"]:
        ok, buf = cv2.imencode(".png", cv2.cvtColor(res["images"]["overlay"], cv2.COLOR_RGB2BGR))
        if ok:
            st.download_button("Download annotated overlay (PNG)", buf.tobytes(), file_name="loopcheck_overlay.png", mime="image/png", key="dl_overlay")
    st.json(payload, expanded=False)
