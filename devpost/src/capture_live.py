"""Capture the four Devpost screenshots from the LIVE LoopCheck app in HTTP API mode.

    uv run --frozen --group demo --with playwright==1.56.0 python devpost/src/capture_live.py
    # options: --url https://loopcheck.streamlit.app  --out devpost/media  --label live

What it does (and refuses to do):
  * Opens the app (Streamlit Community Cloud wraps it in an iframe; both layouts are handled),
    selects **HTTP API**, presses **Check API health** and requires `ok: OpenCV 5`.
  * For each image it selects the bundled case, presses **Run inspection agent**, and reads the
    decision banner back from the page. The capture is ABORTED (nothing written) unless the
    banner shows the expected decision AND "HTTP API", i.e. the result came through the deployed
    API, not the in-process backend.
  * Aborts if the rendered page contains an API Gateway hostname or an editable API URL field
    (the app hides a Secrets-configured endpoint; this double-checks it).
  * Screenshots are genuine pixels at device scale 1. The only edits are a 1800x1200 (3:2) crop
    window chosen from element positions, and hiding Streamlit Cloud's own viewer badge outside
    the app. No overlays, no compositing across runs.
Writes 01-loopcheck-hero.png, 02-visual-evidence.png, 03-agent-trace.png, 04-human-review.png
and capture_log.json (timestamps, decisions, banner texts, crop boxes) into --out.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
VIEW_W, VIEW_H = 1800, 2600   # tall viewport; each image is a 1800x1200 window of it
OUT_W, OUT_H = 1800, 1200
CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
LEAK = re.compile(r"execute-api|amazonaws\.com|\.lambda-url\.", re.I)
SHOTS = [  # file, case title, expected decision, expected rule, tab to open
    ("01-loopcheck-hero.png", "Missing part", "DEFECT", "T1", "Images"),
    ("02-visual-evidence.png", "Missing part", "DEFECT", "T1", "Images"),
    ("03-agent-trace.png", "Defect next to an ArUco marker", "DEFECT", "T1", "Agent trace"),
    ("04-human-review.png", "Ambiguous tiny object", "HUMAN_REVIEW", "T2", "Detected regions"),
]


class Abort(SystemExit):
    pass


def app_frame(page, timeout_s=240):
    """The frame that hosts the Streamlit app (top page locally, an iframe on Community Cloud)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        for btn in ("Yes, get this app back up!", "Wake up"):  # sleeping Community Cloud app
            b = page.get_by_role("button", name=btn)
            if b.count():
                b.first.click()
        for fr in page.frames:
            try:
                if fr.locator('[data-testid="stSidebar"]').count():
                    return fr
            except Exception:
                pass
        page.wait_for_timeout(1000)
    raise Abort("the Streamlit app did not load (sidebar not found)")


def idle(fr, page, extra_ms=700):
    page.wait_for_timeout(300)
    for _ in range(400):
        if fr.locator('[data-testid="stStatusWidget"]').count() == 0 and fr.locator('[data-testid="stSpinner"]').count() == 0:
            break
        page.wait_for_timeout(150)
    page.wait_for_timeout(extra_ms)


def box(loc):
    b = loc.bounding_box()
    if b is None:
        raise Abort(f"element not visible: {loc}")
    return b


def leak_check(fr, where):
    html = fr.content()
    if LEAK.search(html):
        raise Abort(f"{where}: an API hostname is visible in the page; refusing to capture")
    if fr.locator("input[aria-label='API base URL']").count():
        raise Abort(f"{where}: the API URL field is visible (endpoint not configured via Secrets?); refusing to capture")


def banner(fr, decision):
    return fr.locator("div[style*='border-radius: 8px']", has_text=decision).first


def run_case(fr, page, title, decision, rule):
    sel = fr.locator('[data-testid="stSelectbox"]').first
    sel.click()
    fr.get_by_role("option", name=title, exact=True).click()
    idle(fr, page)
    fr.get_by_role("button", name="Run inspection agent").click()
    try:
        banner(fr, decision).wait_for(timeout=120_000)
    except PwTimeout:
        any_banner = fr.locator("div[style*='border-radius: 8px']").first
        seen = any_banner.inner_text() if any_banner.count() else "no decision banner"
        raise Abort(f"{title}: expected {decision}, page shows {seen!r}")
    idle(fr, page, 1500)
    txt = banner(fr, decision).inner_text().replace("\n", " ")
    if f"rule {rule}" not in txt or "HTTP API" not in txt:
        raise Abort(f"{title}: banner {txt!r} lacks 'rule {rule}' or 'HTTP API'")
    return txt


def grab(page, y0, x0=0):
    png = page.screenshot(full_page=False, animations="disabled", caret="hide")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    y0 = int(max(0, min(y0, img.height - OUT_H)))
    x0 = int(max(0, min(x0, img.width - OUT_W)))
    return img.crop((x0, y0, x0 + OUT_W, y0 + OUT_H)), (x0, y0, x0 + OUT_W, y0 + OUT_H)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="https://loopcheck.streamlit.app")
    ap.add_argument("--out", default=str(ROOT / "devpost" / "media"))
    ap.add_argument("--label", default="live", help="recorded in capture_log.json (e.g. live / local-dry-run)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = {"url_host": re.sub(r"^https?://([^/]+).*", r"\1", args.url), "label": args.label,
           "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "shots": []}
    staged = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM if Path(CHROMIUM).exists() else None)
        page = browser.new_page(viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=1, color_scheme="light")
        page.goto(args.url, wait_until="domcontentloaded", timeout=120_000)
        fr = app_frame(page)
        page.add_style_tag(content='[class*="viewerBadge"], [data-testid="manage-app-button"] {display:none !important}')
        idle(fr, page)
        fr.get_by_text("HTTP API", exact=True).click()
        idle(fr, page)
        fr.get_by_role("button", name="Check API health").click()
        try:
            fr.get_by_text(re.compile(r"^ok: OpenCV 5\.")).wait_for(timeout=60_000)
        except PwTimeout:
            raise Abort("API health check did not return 'ok: OpenCV 5.x'; nothing captured")
        log["health"] = fr.get_by_text(re.compile(r"^ok: OpenCV 5\.")).first.inner_text()
        leak_check(fr, "health")

        last_case = None
        for fname, title, decision, rule, tab in SHOTS:
            if title != last_case:
                txt = run_case(fr, page, title, decision, rule)
                last_case = title
            fr.get_by_role("tab", name=tab).click()
            idle(fr, page, 1200)
            if fname.startswith("01"):
                y0 = box(fr.locator("h1").first)["y"] - 36
            elif fname.startswith("02"):
                y0 = box(fr.get_by_role("tab", name="Images"))["y"] - 24
            elif fname.startswith("03"):
                exps = fr.locator('[data-testid="stExpander"]')
                for i in range(exps.count()):   # steps 0-1 open by default: show the adaptive steps instead
                    head = exps.nth(i).locator("summary").first
                    label = head.inner_text()        # starts with the icon name, e.g. "keyboard_arrow_down"
                    if not re.search(r"Step \d+ · rule", label):
                        continue
                    is_open = exps.nth(i).locator("details").first.evaluate("d => d.open")
                    want = bool(re.search(r"rule C1 ", label))   # the re-inspection; B1/F1/E1 stay visible as headers
                    if is_open != want:
                        head.click()
                        idle(fr, page, 400)
                c1 = fr.locator('[data-testid="stExpander"]', has_text="rule C1").first
                js = c1.locator('[data-testid="stJson"]').first   # "Observations used" for the re-inspection
                try:   # open the observations, then their first_pass (peak, area, cue) — real values
                    js.locator(".icon-container").first.click(timeout=3000)
                    idle(fr, page, 400)
                    js.locator(".icon-container").nth(1).click(timeout=3000)
                    idle(fr, page, 400)
                except PwTimeout:
                    pass  # the step stays readable with the observations collapsed
                y0 = box(banner(fr, decision))["y"] - 24
            else:
                exp = fr.locator('[data-testid="stExpander"]', has_text="re-inspection evidence").first
                if exp.count():
                    exp.locator("summary").first.click()
                    idle(fr, page, 500)
                y0 = box(fr.locator("h1").first)["y"] - 36
            leak_check(fr, fname)
            img, crop = grab(page, y0)
            staged[fname] = img
            log["shots"].append({"file": fname, "case": title, "tab": tab, "decision_banner": txt, "crop_xyxy": crop,
                                 "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            print(f"captured {fname}: {txt}")
        browser.close()
    for fname, img in staged.items():   # write only after every check passed
        img.save(out / fname, optimize=True)
    (out / "capture_log.json").write_text(json.dumps(log, indent=1))
    print("wrote", len(staged), "screenshots and capture_log.json to", out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Abort as e:
        print("ABORTED:", e, file=sys.stderr)
        sys.exit(2)
