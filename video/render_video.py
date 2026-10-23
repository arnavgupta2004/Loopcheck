#!/usr/bin/env python3
"""Reproducible render entry point for the LoopCheck demo film.

    uv run --frozen python video/render_video.py                 # full render -> video/loopcheck_demo.mp4
    uv run --frozen python video/render_video.py --preview       # fast 960x540 / 15 fps preview
    uv run --frozen python video/render_video.py --stills 5,24,44.8   # PNG stills -> video/build/stills/
    uv run --frozen python video/render_video.py --voiceover my_read.wav   # final export WITH narration
    uv run --frozen python video/render_video.py --assets        # regenerate pipeline imagery (real service)
    uv run --frozen python video/render_video.py --remux         # audio/SFX/doc changes only: rebuild stems, re-mux

Video: 1920x1080, 30 fps, H.264 High (yuv420p, BT.709), MP4 with +faststart.
Audio streams in loopcheck_demo.mp4 (AAC 48 kHz stereo):
  #0 "Music + SFX (no narration)"  default
  #1 "Music stem"   #2 "SFX stem"   #3 "Voice-over placeholder (silent)"
Narration is never mixed in unless --voiceover is given. No network access is needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from multiprocessing import Pool
from pathlib import Path

VIDEO = Path(__file__).resolve().parent
ROOT = VIDEO.parent
sys.path.insert(0, str(VIDEO / "src"))

import timeline as T  # noqa: E402

BUILD = VIDEO / "build"
AUDIO = VIDEO / "audio"
OUT = VIDEO / "loopcheck_demo.mp4"


# ----------------------------------------------------------------------------- frames
_scale = None


def _init(scale):
    global _scale
    _scale = scale
    import scenes  # noqa: F401  (warm caches per worker)


def _render(i_fps):
    i, fps = i_fps
    import scenes
    from PIL import Image
    img = scenes.frame(i / fps).convert("RGB")
    if _scale != (T.WIDTH, T.HEIGHT):
        img = img.resize(_scale, Image.LANCZOS)
    return img.tobytes()


def render_video(path: Path, fps: int, size: tuple[int, int], workers: int, t0: float = 0.0, t1: float = T.DURATION) -> None:
    n0, n1 = int(round(t0 * fps)), int(round(t1 * fps))
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
           "-r", str(fps), "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           "-c:v", "libx264", "-preset", "slow" if size[0] >= 1920 else "veryfast", "-crf", "16" if size[0] >= 1920 else "23",
           "-profile:v", "high", "-tune", "film", "-g", str(fps * 2), "-bf", "2",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-movflags", "+faststart", str(path)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    start = time.time()
    with Pool(workers, initializer=_init, initargs=(size,)) as pool:
        for k, buf in enumerate(pool.imap(_render, [(i, fps) for i in range(n0, n1)], chunksize=6)):
            enc.stdin.write(buf)
            if k % (fps * 5) == 0:
                done = (k + 1) / (n1 - n0)
                print(f"  frames {k + 1}/{n1 - n0}  ({done:.0%}, {time.time() - start:.0f}s)", flush=True)
    enc.stdin.close()
    if enc.wait() != 0:
        raise SystemExit("ffmpeg video encode failed")


def mux(video: Path, out: Path) -> None:
    streams = [("mix_no_vo.wav", "Music + SFX (no narration)"), ("music.wav", "Music stem"), ("sfx.wav", "SFX stem"),
               ("voiceover.wav", "Voice-over placeholder (silent)")]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    for f, _ in streams:
        cmd += ["-i", str(AUDIO / f)]
    cmd += ["-map", "0:v"] + sum([["-map", f"{i + 1}:a"] for i in range(len(streams))], [])
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-t", f"{T.DURATION:.3f}"]
    for i, (_, title) in enumerate(streams):
        cmd += [f"-metadata:s:a:{i}", f"title={title}", f"-metadata:s:a:{i}", f"handler_name={title}",
                f"-disposition:a:{i}", "default" if i == 0 else "0"]
    cmd += ["-metadata", "title=LoopCheck — agentic visual inspection (research prototype)", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)


def export_with_voiceover(vo: Path, out: Path) -> None:
    """Final export: recorded narration over the music+SFX bed, bed ducked under speech (sidechain)."""
    if not OUT.exists():
        raise SystemExit(f"{OUT} not found: run the full render first")
    fc = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{T.DURATION},asplit=2[vo][key];"
          "[2:a]aformat=channel_layouts=stereo[bed];"
          "[bed][key]sidechaincompress=threshold=0.03:ratio=5:attack=15:release=350:makeup=1[duck];"
          "[duck][vo]amix=inputs=2:normalize=0,alimiter=limit=0.89[mix]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(OUT), "-i", str(vo), "-i", str(AUDIO / "mix_no_vo.wav"),
           "-filter_complex", fc, "-map", "0:v", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
           "-metadata:s:a:0", "title=Narration + music + SFX", "-t", f"{T.DURATION:.3f}", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    print("wrote", out)


# ----------------------------------------------------------------------------- documents
def tc(s: float) -> str:
    return f"{int(s // 60):02d}:{s % 60:05.2f}"


def write_timing_csv(path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scene_id", "start_s", "end_s", "start_tc", "end_tc", "vo_start_s", "vo_end_s", "words", "narration", "visual_cue"])
        for s in T.SCENES:
            w.writerow([s["id"], f"{s['start']:.2f}", f"{s['end']:.2f}", tc(s["start"]), tc(s["end"]), f"{s['vo'][0]:.2f}",
                        f"{s['vo'][1]:.2f}", T.words(s["text"]), s["text"], s["cue"]])


def write_script_md(path: Path) -> None:
    total = T.total_words()
    spoken = sum(s["vo"][1] - s["vo"][0] for s in T.SCENES)
    lines = [
        "# LoopCheck demo film: voice-over script",
        "",
        f"Total: **{total} words** across {spoken:.0f} s of narration windows in an {T.DURATION:.0f} s film "
        f"(≈{total / spoken * 60:.0f} words per minute inside the windows). Generated from `video/src/timeline.py`; "
        "edit the text and timings there, then re-render.",
        "",
        "## Delivery notes",
        "",
        "- Calm, precise, unhurried. Technical documentary, not an advert. No hype words.",
        "- Pronunciation: *LoopCheck* (one word, stress on Loop); *OpenCV* = \"open C V\"; *HUMAN_REVIEW* is read \"human review\".",
        "- `/` marks a short breath; `//` a deliberate pause (≈0.6 s) for a visual beat.",
        "- Keep the qualification in scene S6 audible and unrushed: it is a required part of the claim.",
        "- Record 48 kHz / 24-bit mono or stereo WAV, about 1 s of room tone at the head, then start at 00:00.5.",
        "",
        "## Script",
        "",
    ]
    marks = {
        "S1": "A missing part may be a few dozen pixels. // Inspection asks: / does what the camera sees / match what should be there?",
        "S2": "LoopCheck is a research prototype for agentic visual inspection. // Rather than one opaque label, / it returns a decision / and its evidence.",
        "S3": "Its agent is a rule-based controller, / not a neural network. // It calls OpenCV tools in turn: / register the images, // then compare them into an anomaly map. // Extra checks run only when the evidence calls for them.",
        "S4": "Here is the running app. // An unchanged tray is approved. // A missing component is a confirmed defect. // A faint, tiny object can't be confirmed, / so the agent escalates to human review / instead of guessing.",
        "S5": "Strong evidence is confirmed directly; / weaker regions must be reproduced by independent views, / or go to a person. // The same controller runs on AWS Lambda behind API Gateway, / returning the decision, its rule / and the tool trace.",
        "S6": "On a held-out synthetic test set, / it made no automatic errors / and sent about a third of cases to review. // That's synthetic validation, / not real-world performance, / and the scores aren't probabilities.",
        "S7": "Visual evidence. // Explicit decisions. // Inspectable execution.",
    }
    for s in T.SCENES:
        assert marks[s["id"]].replace("// ", "").replace("/ ", "").replace(" //", "").replace(" /", "") .replace("  ", " ") \
            .strip().split() == s["text"].split(), f"read marks for {s['id']} drifted from timeline text"
        lines += [f"### {s['id']} · {s['label']} · {tc(s['start'])}–{tc(s['end'])} (speak {tc(s['vo'][0])}–{tc(s['vo'][1])}, "
                  f"{T.words(s['text'])} words)", "", f"> {marks[s['id']]}", "", f"*On screen:* {s['cue']}", ""]
    lines += [
        "## Accuracy notes (what each claim rests on)",
        "",
        "- *Rule-based controller, not a neural network*: `loopcheck/agent.py` (ordered policy rules R0, R1, A–G, C0/C1, T1–T5, X1–X4).",
        "- *Register / compare*: ArUco or SIFT + USAC-MAGSAC homography (`registration.py`); fused intensity, SSIM, gradient and colour cues (`compare.py`).",
        "- *Strong evidence confirmed directly*: rule C0 (peak ≥ 0.75, area ≥ 400 px, no illumination shift); weaker candidates need C1 re-inspection (structure view ≥ 0.70 and ≥ 1 other view).",
        "- *Approved / defect / human review*: bundled cases 01 (APPROVE T5), 02 (DEFECT T1), 05 (HUMAN_REVIEW T2), re-run and read back from the app at capture time.",
        "- *Held-out synthetic test set*: `results/d3/test/metrics.json`: 180 cases, 0 errors among automatic decisions, HUMAN_REVIEW rate 0.361.",
        "- *AWS Lambda behind API Gateway*: `template.yaml` (HTTP API → Lambda python3.13, OpenCV 5). The on-screen terminal shows the smoke test against "
        "the same handler served locally unless `video/assets/api/smoke_deployed.txt` is provided (see README).",
        "",
    ]
    path.write_text("\n".join(lines))


# ----------------------------------------------------------------------------- QA
def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def contact_sheet(video: Path, out: Path, times: list[float]) -> None:
    from PIL import Image, ImageDraw, ImageFont
    tmp = BUILD / "contact"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    tiles = []
    for t in times:
        f = tmp / f"{t:06.2f}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(f)], check=True)
        tiles.append((t, Image.open(f).convert("RGB").resize((480, 270), Image.LANCZOS)))
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 480 + (cols + 1) * 16, rows * (270 + 40) + 16), (14, 15, 17))
    d = ImageDraw.Draw(sheet)
    fnt = ImageFont.truetype(str(VIDEO / "assets" / "fonts" / "JetBrainsMono-Regular.ttf"), 16)
    for k, (t, im) in enumerate(tiles):
        x, y = 16 + (k % cols) * (480 + 16), 16 + (k // cols) * (270 + 40)
        sheet.paste(im, (x, y))
        sid = next(s["id"] for s in T.SCENES if s["start"] <= t < s["end"] or s is T.SCENES[-1])
        d.text((x, y + 276), f"{tc(t)}  {sid}", font=fnt, fill=(160, 166, 175))
    sheet.save(out, quality=90)


def blank_check(video: Path, fps: int) -> list[str]:
    """Report dark stretches longer than 0.5 s outside the intended dips (scene changes, head, tail)."""
    out = subprocess.run(["ffmpeg", "-i", str(video), "-vf", "blackdetect=d=0.5:pic_th=0.98:pix_th=0.06", "-an", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return [ln.split("]")[-1].strip() for ln in out.splitlines() if "black_start" in ln]


# ----------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", action="store_true", help="960x540 at 15 fps -> video/build/preview.mp4")
    ap.add_argument("--stills", help="comma-separated times (s) -> video/build/stills/*.png")
    ap.add_argument("--voiceover", help="recorded narration WAV/MP3/M4A -> video/loopcheck_demo_with_vo.mp4")
    ap.add_argument("--assets", action="store_true", help="regenerate video/assets/pipeline from the real service")
    ap.add_argument("--remux", action="store_true", help="rebuild audio + docs only and re-mux the last rendered picture")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=T.DURATION)
    args = ap.parse_args(argv)

    if args.assets:
        subprocess.run([sys.executable, str(VIDEO / "src" / "make_assets.py")], check=True)
        return 0
    if args.voiceover:
        export_with_voiceover(Path(args.voiceover), VIDEO / "loopcheck_demo_with_vo.mp4")
        return 0
    if args.stills:
        import scenes
        d = BUILD / "stills"
        d.mkdir(parents=True, exist_ok=True)
        for t in (float(x) for x in args.stills.split(",")):
            scenes.frame(t).convert("RGB").save(d / f"t{t:06.2f}.png")
            print("still", d / f"t{t:06.2f}.png")
        return 0

    needed = [VIDEO / "assets" / "pipeline" / k / f"{n}.png" for k in ("clean", "missing", "ambiguous")
              for n in ("reference", "inspection", "aligned", "heatmap", "overlay")]
    if not all(f.exists() for f in needed):
        print("pipeline imagery missing: generating it with the real service (make_assets.py)")
        subprocess.run([sys.executable, str(VIDEO / "src" / "make_assets.py")], check=True)
    write_timing_csv(VIDEO / "voiceover_timing.csv")
    write_script_md(VIDEO / "voiceover_script.md")
    import audio
    print("audio stems:", audio.build(AUDIO))
    shutil.copy(AUDIO / "voiceover.wav", VIDEO / "voiceover.wav")

    if args.remux:
        silent = BUILD / "video_noaudio.mp4"
        if not silent.exists():
            raise SystemExit("no rendered picture yet: run the full render first")
        mux(silent, OUT)
        print("re-muxed", OUT)
        return 0

    if args.preview:
        p = BUILD / "preview.mp4"
        render_video(p, 15, (960, 540), args.workers, args.start, args.end)
        print("wrote", p)
        return 0

    t0 = time.time()
    silent = BUILD / "video_noaudio.mp4"
    render_video(silent, T.FPS, (T.WIDTH, T.HEIGHT), args.workers, args.start, args.end)
    mux(silent, OUT)
    info = probe(OUT)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    auds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    report = {"file": str(OUT.relative_to(ROOT)), "duration_s": float(info["format"]["duration"]), "size_mb": round(int(info["format"]["size"]) / 1e6, 1),
              "video": {"codec": v["codec_name"], "profile": v.get("profile"), "width": v["width"], "height": v["height"],
                        "fps": v["r_frame_rate"], "frames": int(v.get("nb_frames", 0)), "pix_fmt": v["pix_fmt"], "color": v.get("color_space")},
              "audio": [{"codec": a["codec_name"], "sample_rate": a["sample_rate"], "channels": a["channels"],
                         "title": a.get("tags", {}).get("handler_name")} for a in auds],
              "render_seconds": round(time.time() - t0, 1)}
    times = []
    for s in T.SCENES:
        d = s["end"] - s["start"]
        times += [round(s["start"] + d * f, 2) for f in (0.3, 0.75)]
    contact_sheet(OUT, VIDEO / "contact_sheet.jpg", times)
    report["dark_stretches"] = blank_check(OUT, T.FPS)
    (BUILD / "render_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
