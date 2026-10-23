"""Build devpost/LoopCheck-submission-materials.zip (judge-ready supplementary package).

    uv run --frozen --group demo --with playwright==1.56.0 python devpost/src/build_submission.py
    # optional: --deployed-smoke path/to/deployed_smoke_output.txt   (sanitised before inclusion)

Steps (any failed check aborts and no ZIP is written):
  1. regenerate the architecture and evaluation graphics (make_graphics.py; metrics read
     from results/d3/test/metrics.json);
  2. crop two genuine app screenshots from video/assets/app (captured from the running app);
  3. re-run all 8 bundled cases with loopcheck.service.analyze and require the decisions
     documented in REPRODUCTION.md;
  4. check every number in the report's results table against metrics.json;
  5. render technical_report.md -> HTML (pandoc) -> PDF (headless Chromium);
  6. scan every packaged text for credentials, account ids, endpoints and secret files;
  7. write the ZIP, then re-open it: CRC test, file list, size < 35 MB, PDF text checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "devpost" / "submission"
BUILD = ROOT / "devpost" / "build" / "submission"
PKG = BUILD / "LoopCheck-submission-materials"
ZIP = ROOT / "devpost" / "LoopCheck-submission-materials.zip"
LIMIT = 35 * 1024 * 1024
CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "devpost" / "src"))

EXPECTED_CASES = {  # REPRODUCTION.md table
    "01_clean": ("APPROVE", "T5"), "02_missing_part": ("DEFECT", "T1"), "03_shifted_part": ("DEFECT", "T1"),
    "04_lighting_change": ("APPROVE", "T5"), "05_ambiguous_tiny": ("HUMAN_REVIEW", "T2"),
    "06_defect_near_marker": ("DEFECT", "T1"), "07_different_tray": ("HUMAN_REVIEW", "F2"),
    "08_registration_failure": ("HUMAN_REVIEW", "A2"),
}
SECRET_PATTERNS = [
    (r"(?<![A-Z0-9])(AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])", "AWS access key id"),
    (r"aws_secret_access_key|aws_session_token", "AWS secret field"),
    (r"(?<![\d.])\d{12}(?![\d.])", "12-digit number (possible AWS account id)"),
    (r"[a-z0-9]{8,12}\.execute-api\.[a-z0-9-]+\.amazonaws\.com", "API Gateway endpoint"),
    (r"arn:aws:iam::|:user/", "IAM principal / ARN"),
    (r"LOOPCHECK_API_URL\s*=\s*\"https", "configured endpoint value"),
    (r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY", "private key"),
]


class BuildError(SystemExit):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise BuildError(f"BUILD FAILED: {msg}")


def crop_app_figures(dst: Path) -> None:
    cap = json.loads((ROOT / "video" / "assets" / "app" / "capture.json").read_text())
    # case 02: title .. bottom of the result-image row (main column only)
    b = cap["missing_images"]["boxes"]
    title = cap["landing_eval"]["boxes"]["title"]
    bottom = max(i["y"] + i["height"] for i in b["images"]) + 150
    img = Image.open(ROOT / "video" / "assets" / "app" / "missing_images.jpg").convert("RGB")
    img.crop((700, title["y"] - 50, 3100, bottom)).resize((1500, int((bottom - title["y"] + 50) * 1500 / 2400)), Image.LANCZOS) \
        .save(dst / "app_missing_part.jpg", quality=88, optimize=True)
    # case 05 trace: decision banner .. final decision line
    t = cap["ambiguous_trace"]["boxes"]
    top = t["banner"]["y"] - 40
    last = max(s["y"] + s["height"] for s in t["steps"] if s) + 110  # None = expander in a hidden tab
    img = Image.open(ROOT / "video" / "assets" / "app" / "ambiguous_trace.jpg").convert("RGB")
    img.crop((700, top, 3100, last)).resize((1500, int((last - top) * 1500 / 2400)), Image.LANCZOS) \
        .save(dst / "app_agent_trace.jpg", quality=88, optimize=True)


def verify_bundled_cases() -> list[dict]:
    from loopcheck.service import analyze
    out = []
    for slug, (dec, rule) in EXPECTED_CASES.items():
        d = ROOT / "data" / "demo_cases" / slug
        r, _ = analyze((d / "reference.png").read_bytes(), (d / "inspection.png").read_bytes(), include_images=False)
        steps = [f"{s['rule']}:{s['tool']}" for s in r.get("report", {}).get("steps", [])]
        out.append({"case": slug, "decision": r["decision"], "termination_rule": r["termination_rule"], "tool_sequence": steps,
                    "opencv_version": r["runtime"]["opencv_version"]})
        check((r["decision"], r["termination_rule"]) == (dec, rule), f"{slug}: got {r['decision']} {r['termination_rule']}, documented {dec} {rule}")
    return out


def verify_report_numbers(md: str) -> None:
    m = json.loads((ROOT / "results" / "d3" / "test" / "metrics.json").read_text())
    o, a = m["one_shot"], m["agent"]
    f3 = lambda x: f"{x:.3f}"  # noqa: E731
    rows = [
        f"| {f3(o['image_level']['precision'])} / {f3(o['image_level']['recall'])} / {f3(o['image_level']['f1'])} | "
        f"{f3(a['image_level']['precision'])} / {f3(a['image_level']['recall'])} / {f3(a['image_level']['f1'])} |",
        f"| {f3(o['false_positive_rate_good'])} | {f3(a['false_positive_rate_good'])} |",
        f"| {f3(o['good_not_auto_approved_rate'])} | {f3(a['good_not_auto_approved_rate'])} |",
        f"| {f3(o['automatic_coverage'])} | {f3(a['automatic_coverage'])} |",
        f"| {f3(o['automatic_accuracy'])} | {f3(a['automatic_accuracy'])} |",
        f"| {o['automatic_errors']} | {a['automatic_errors']} |",
        f"| {o['critical_errors_approved']} | {a['critical_errors_approved']} |",
        f"| {f3(o['human_review_rate'])} | {f3(a['human_review_rate'])} |",
        f"| {f3(o['task_success_rate'])} | {f3(a['task_success_rate'])} |",
        f"| {o['seconds']['mean']:.2f} / {o['seconds']['median']:.2f} / {o['seconds']['p90']:.2f} s | "
        f"{a['seconds']['mean']:.2f} / {a['seconds']['median']:.2f} / {a['seconds']['p90']:.2f} s |",
        f"n = {a['n']}",
    ]
    for r in rows:
        check(r in md, f"report table out of sync with metrics.json: expected fragment {r!r}")


def sanitise_deployed(text: str) -> str:
    text = re.sub(r"https?://[^\s\"']+", "<API_URL>", text)
    return re.sub(r"(?<![\d.])\d{12}(?![\d.])", "<ACCOUNT_ID>", text)


def render_pdf(html: Path, pdf: Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM if Path(CHROMIUM).exists() else None)
        page = browser.new_page()
        page.goto(html.as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.pdf(path=str(pdf), format="A4", print_background=True, prefer_css_page_size=True, display_header_footer=True,
                 header_template="<span></span>",
                 footer_template='<div style="font-family:sans-serif;font-size:7.5pt;color:#7a808a;width:100%;padding:0 17mm;'
                                 'display:flex;justify-content:space-between"><span>LoopCheck · technical report · synthetic-data '
                                 'evaluation, research prototype</span><span><span class="pageNumber"></span> / '
                                 '<span class="totalPages"></span></span></div>')
        browser.close()


def secret_scan(root: Path) -> None:
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in {".md", ".txt", ".json", ".html", ".css"}:
            continue
        text = f.read_text(errors="ignore")
        for pat, what in SECRET_PATTERNS:
            mt = re.search(pat, text)
            check(mt is None, f"{f.relative_to(root)}: {what} found ({mt.group(0) if mt else ''!r})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deployed-smoke", type=Path, help="output of `python -m loopcheck.client --url <ApiUrl> ...` from the deployed API")
    args = ap.parse_args(argv)

    shutil.rmtree(BUILD, ignore_errors=True)
    (PKG / "figures").mkdir(parents=True)
    (PKG / "evidence").mkdir(parents=True)

    import make_graphics
    make_graphics.architecture()
    make_graphics.evaluation()
    shutil.copy(ROOT / "devpost" / "media" / "05-loopcheck-architecture.png", PKG / "figures" / "architecture.png")
    shutil.copy(ROOT / "devpost" / "media" / "06-evaluation-results.png", PKG / "figures" / "evaluation_results.png")
    crop_app_figures(PKG / "figures")

    cases = verify_bundled_cases()
    (PKG / "evidence" / "bundled_cases_verification.json").write_text(json.dumps(
        {"note": "Re-run with loopcheck.service.analyze (the code the Lambda runs) when this package was built. Synthetic cases.",
         "cases": cases}, indent=1))
    print("bundled cases verified:", ", ".join(f"{c['case']}={c['decision']}/{c['termination_rule']}" for c in cases))

    shutil.copy(ROOT / "results" / "d3" / "test" / "metrics.json", PKG / "evidence" / "metrics_d3_test.json")
    shutil.copy(ROOT / "results" / "opencv5_check.json", PKG / "evidence" / "opencv5_check.json")
    shutil.copy(ROOT / "video" / "assets" / "api" / "health_local.json", PKG / "evidence" / "health_local.json")
    smoke = (ROOT / "video" / "assets" / "api" / "smoke_local.txt").read_text()
    check("SMOKE TEST: PASS" in smoke, "local smoke-test evidence does not end in PASS")
    (PKG / "evidence" / "smoke_test_local_handler.txt").write_text(
        "# loopcheck.client smoke test against loopcheck.local_server (serves loopcheck.aws_lambda.handler on localhost)\n"
        "# command: python -m loopcheck.client --url http://127.0.0.1:8765 --case data/demo_cases/02_missing_part --expect DEFECT\n"
        + smoke)
    if args.deployed_smoke:
        dep = sanitise_deployed(args.deployed_smoke.read_text())
        check("SMOKE TEST: PASS" in dep, "the deployed smoke-test file does not contain 'SMOKE TEST: PASS'")
        (PKG / "evidence" / "smoke_test_deployed.txt").write_text("# deployed API smoke test (endpoint redacted)\n" + dep)

    md = (SRC / "technical_report.md").read_text()
    verify_report_numbers(md)
    for name in ("technical_report.md", "REPRODUCTION.md", "README.md"):
        shutil.copy(SRC / name, PKG / name)

    # HTML (build dir only) -> PDF
    work = BUILD / "html"
    shutil.copytree(PKG / "figures", work / "figures")
    shutil.copytree(ROOT / "video" / "assets" / "fonts", work / "fonts", ignore=shutil.ignore_patterns("*.txt"))
    shutil.copy(SRC / "report.css", work / "report.css")
    html = work / "technical_report.html"
    subprocess.run(["pandoc", str(SRC / "technical_report.md"), "-f", "markdown+lists_without_preceding_blankline",
                    "-s", "-o", str(html), "--css", "report.css",
                    "--metadata", "lang=en", "--resource-path", str(work)], check=True, cwd=work)
    render_pdf(html, PKG / "technical_report.pdf")

    secret_scan(PKG)
    for f in PKG.rglob("*"):
        check(f.suffix.lower() != ".zip", "nested ZIP in package (e.g. a Lambda deployment zip)")

    ZIP.unlink(missing_ok=True)
    files = sorted(p for p in PKG.rglob("*") if p.is_file())
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            zi = zipfile.ZipInfo(str(f.relative_to(BUILD)), date_time=(2026, 10, 23, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, f.read_bytes(), compresslevel=9)

    # verification of the written archive
    with zipfile.ZipFile(ZIP) as z:
        check(z.testzip() is None, "CRC test failed")
        names = z.namelist()
        total = sum(i.file_size for i in z.infolist())
    size = ZIP.stat().st_size
    check(size < LIMIT, f"ZIP is {size} bytes, over the 35 MB limit")
    required = ["README.md", "technical_report.md", "technical_report.pdf", "REPRODUCTION.md", "figures/architecture.png",
                "figures/evaluation_results.png", "evidence/metrics_d3_test.json"]
    for r in required:
        check(f"LoopCheck-submission-materials/{r}" in names, f"missing {r}")
    pdf = PKG / "technical_report.pdf"
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    flat = " ".join(text.split())
    for phrase in ("not probabilities", "synthetic", "No neural network was trained", "HUMAN_REVIEW", "OpenCV 5",
                   "https://loopcheck.streamlit.app", "https://github.com/arnavgupta2004/Loopcheck"):
        check(phrase.lower() in flat.lower(), f"PDF text lacks {phrase!r}")
    for pat, what in SECRET_PATTERNS:
        check(re.search(pat, text) is None, f"PDF contains {what}")
    print(f"\nZIP {ZIP.relative_to(ROOT)}: {size / 1e6:.2f} MB compressed, {total / 1e6:.2f} MB uncompressed, {len(names)} files, "
          f"sha256 {hashlib.sha256(ZIP.read_bytes()).hexdigest()[:16]}…")
    print(f"PDF: {pages} pages; required phrases present; no secrets found")
    for n in names:
        print("  ", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
