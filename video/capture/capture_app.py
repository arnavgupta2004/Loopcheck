"""Capture authentic screenshots of the running LoopCheck Streamlit app (no mock-ups).

    # terminal 1: the real app (Streamlit's built-in dark theme, minimal toolbar)
    uv run --frozen --group demo streamlit run app/streamlit_app.py --server.headless true \
        --server.port 8501 --browser.gatherUsageStats false --client.toolbarMode minimal --theme.base dark
    # terminal 2
    uv run --frozen --group demo --with playwright==1.56.0 python video/capture/capture_app.py

Drives the real UI with Playwright (select case -> "Run inspection agent" -> wait for the
decision banner -> open tabs) and writes, for each step, a 2x-DPI screenshot plus the
bounding boxes of the controls and result panels (video/assets/app/*.jpg + capture.json).
The decision shown in each screenshot is read back from the page and compared with the
expected decision; the capture FAILS if they differ, so the video can never show an
outcome the app did not produce. Backend: "Local (in-process)" (the deployed endpoint is
not reachable from the capture machine; see video/README.md).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "video" / "assets" / "app"
VIEW_W, VIEW_H, DPR = 1600, 2000, 2
CASES = [  # (key, case title in the selectbox, expected decision, expected rule)
    ("clean", "Clean (identical)", "APPROVE", "T5"),
    ("missing", "Missing part", "DEFECT", "T1"),
    ("ambiguous", "Ambiguous tiny object", "HUMAN_REVIEW", "T2"),
]


def box(locator) -> dict | None:
    b = locator.bounding_box()
    return None if b is None else {k: round(v * DPR, 1) for k, v in b.items()}


def shot(page, name: str, boxes: dict, meta: dict, record: dict) -> None:
    png = page.screenshot(full_page=False, animations="disabled", caret="hide")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.save(OUT / f"{name}.jpg", quality=93, subsampling=0, optimize=True)
    record[name] = {"size": list(img.size), "boxes": boxes, **meta}
    print(f"captured {name} {img.size}")


def wait_idle(page, timeout_s: float = 60) -> None:
    """Streamlit shows a 'running' status widget while a script reruns."""
    t0 = time.time()
    page.wait_for_timeout(400)
    while time.time() - t0 < timeout_s:
        if page.locator('[data-testid="stStatusWidget"]').count() == 0 and page.locator('[data-testid="stSpinner"]').count() == 0:
            page.wait_for_timeout(600)
            return
        page.wait_for_timeout(200)
    raise TimeoutError("app did not become idle")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8501")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    record: dict = {"app_url": "local", "viewport_css": [VIEW_W, VIEW_H], "device_scale_factor": DPR,
                    "backend": "Local (in-process)", "captured_unix": int(time.time())}
    exe = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe if os.path.exists(exe) else None)
        ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=DPR, color_scheme="dark")
        page = ctx.new_page()
        page.goto(args.url, wait_until="networkidle")
        page.get_by_text("Run the controller").wait_for(timeout=60000)
        wait_idle(page)

        # Landing page with the real aggregate held-out (synthetic) evaluation table opened.
        page.get_by_text("Aggregate held-out evaluation").click()
        wait_idle(page)
        shot(page, "landing_eval", {
            "title": box(page.locator("h1").first),
            "warning": box(page.locator('[data-testid="stAlert"]').first),
            "eval_table": box(page.locator('[data-testid="stTable"]').first),
            "sidebar": box(page.locator('[data-testid="stSidebar"]')),
            "local_radio": box(page.get_by_text("Local (in-process)")),
        }, {}, record)

        for key, title, expected, rule in CASES:
            sel = page.locator('[data-testid="stSelectbox"]').first
            sel_box = box(sel)
            sel.click()
            page.get_by_role("option", name=title, exact=True).click()
            wait_idle(page)
            run_btn = page.get_by_role("button", name="Run inspection agent")
            run_box = box(run_btn)
            run_btn.click()
            banner = page.get_by_text(expected, exact=True).first
            banner.wait_for(timeout=90000)
            wait_idle(page)
            # the app's decision banner: <div style="...border-radius:8px;background:...">DECISION rule ... · backend · wall</div>
            banner_div = page.locator("div[style*='border-radius: 8px']", has_text=expected).first
            txt = banner_div.inner_text()
            if expected not in txt or f"rule {rule}" not in txt or "Local (in-process)" not in txt:
                raise SystemExit(f"{key}: page shows {txt!r}, expected {expected} rule {rule}")
            imgs = page.locator('[data-testid="stImage"]')
            boxes = {"case_select": sel_box, "run_button": run_box, "banner": box(banner_div),
                     "images": [box(imgs.nth(i)) for i in range(imgs.count())],
                     "reasons": box(page.locator('[data-testid="stMarkdownContainer"] ul').first)}
            shot(page, f"{key}_images", boxes, {"decision": expected, "rule": rule, "banner_text": txt.strip()}, record)

            page.get_by_role("tab", name="Agent trace").click()
            wait_idle(page)
            exps = page.locator('[data-testid="stExpander"]')
            shot(page, f"{key}_trace", {"banner": box(banner_div), "trace_tab": box(page.get_by_role("tab", name="Agent trace")),
                                        "steps": [box(exps.nth(i)) for i in range(exps.count())]},
                 {"decision": expected, "rule": rule}, record)
            page.get_by_role("tab", name="Images").click()
            wait_idle(page)
        browser.close()
    (OUT / "capture.json").write_text(json.dumps(record, indent=1))
    print("wrote", OUT / "capture.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
