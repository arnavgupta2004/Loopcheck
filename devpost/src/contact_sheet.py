"""Contact sheet of the six Devpost images + a QA table (size, resolution, aspect, file size).

    uv run --frozen python devpost/src/contact_sheet.py

Images that do not exist yet get an explicit "NOT CAPTURED" tile; nothing is substituted.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "devpost" / "media"
FONTS = ROOT / "video" / "assets" / "fonts"
FILES = ["01-loopcheck-hero.png", "02-visual-evidence.png", "03-agent-trace.png", "04-human-review.png",
         "05-loopcheck-architecture.png", "06-evaluation-results.png"]
TW, TH, PAD, CAP = 600, 400, 24, 56
LIMIT = 5 * 1024 * 1024


def qa(path: Path) -> dict:
    with Image.open(path) as im:
        w, h = im.size
    size = path.stat().st_size
    return {"file": path.name, "w": w, "h": h, "aspect": round(w / h, 3), "mb": round(size / 1e6, 2),
            "ok": abs(w / h - 1.5) < 0.01 and size < LIMIT}


def main() -> int:
    f = ImageFont.truetype(str(FONTS / "JetBrainsMono-Medium.ttf"), 15)
    big = ImageFont.truetype(str(FONTS / "Inter-SemiBold.otf"), 20)
    sheet = Image.new("RGB", (3 * TW + 4 * PAD, 2 * (TH + CAP) + 3 * PAD), (11, 12, 14))
    d = ImageDraw.Draw(sheet)
    rows = []
    for i, name in enumerate(FILES):
        x, y = PAD + (i % 3) * (TW + PAD), PAD + (i // 3) * (TH + CAP + PAD)
        p = MEDIA / name
        if p.exists():
            r = qa(p)
            rows.append(r)
            sheet.paste(Image.open(p).convert("RGB").resize((TW, TH), Image.LANCZOS), (x, y))
            cap = f"{name}  ·  {r['w']}x{r['h']}  ·  {r['mb']} MB"
        else:
            d.rectangle([x, y, x + TW, y + TH], fill=(24, 18, 19), outline=(229, 72, 77), width=2)
            d.text((x + TW / 2, y + TH / 2 - 16), "NOT CAPTURED", font=big, fill=(240, 170, 170), anchor="mm")
            d.text((x + TW / 2, y + TH / 2 + 16), "live app unreachable from the build environment", font=f, fill=(170, 176, 186), anchor="mm")
            cap = f"{name}  ·  pending: run devpost/src/capture_live.py"
        d.text((x, y + TH + 12), cap, font=f, fill=(170, 176, 186))
    out = MEDIA / "contact-sheet.png"
    sheet.save(out, optimize=True)
    for r in rows:
        print(f"{r['file']:32s} {r['w']}x{r['h']}  aspect {r['aspect']}  {r['mb']} MB  {'OK' if r['ok'] else 'CHECK'}")
    print("wrote", out.relative_to(ROOT), f"({out.stat().st_size / 1e6:.2f} MB); missing:",
          [n for n in FILES if not (MEDIA / n).exists()] or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
