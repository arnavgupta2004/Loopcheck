# LoopCheck demo film

An 88-second, 1920×1080 / 30 fps technical film introducing LoopCheck, built entirely from
the real system. Narration is recorded separately: the film ships with a silent voice-over
track, a timed script and a one-command export that lays your recording over the music and
effects bed.

| Deliverable | Where | In Git? |
|---|---|---|
| Final film (no narration) | `video/loopcheck_demo.mp4` | no: rebuild it (≈2 min) |
| Voice-over script | `video/voiceover_script.md` | yes |
| Timed narration + visual cues | `video/voiceover_timing.csv` | yes |
| Silent voice-over placeholder (88.0 s, 48 kHz) | `video/voiceover.wav` | no: rebuilt with the film |
| Render entry point | `video/render_video.py` | yes |
| Editable source | `video/src/` (`timeline.py`, `scenes.py`, `gfx.py`, `audio.py`, `make_assets.py`) | yes |
| App capture script and captures | `video/capture/capture_app.py`, `video/assets/app/` | yes |
| Contact sheet (2 frames per scene) | `video/contact_sheet.jpg` | yes |

## What is real, and what is presentation

- **App footage:** genuine screenshots of the running Streamlit app (`app/streamlit_app.py`),
  captured with Playwright by `capture/capture_app.py`.
  - The script picks each case through the real UI and presses **Run inspection agent**.
  - It then **reads the decision banner back from the page** and fails if it differs from the
    expected outcome (APPROVE T5, DEFECT T1, HUMAN_REVIEW T2).
  - The app ran with Streamlit's built-in dark theme (`--theme.base dark`) and a minimal
    toolbar. Nothing in the UI was edited.
  - **The pointer is drawn by the renderer** on the real control positions recorded at
    capture time; an on-screen caption says so. Clicks cut to the resulting screenshot.
- **Workflow, evidence and close-ups:** these use real outputs from `loopcheck.service.analyze`
  (the code the app and Lambda run), produced by `src/make_assets.py`:
  - aligned images, heatmaps and overlays;
  - the estimated homography, which drives the registration animation;
  - candidate boxes and scores, tool sequences and reasons.
- **Opening and closing macro:** the real inspection image of demo case 02, graded and
  re-framed. The red bracket is the ground-truth missing-part box mapped through the
  *estimated* homography. It is labelled on screen as a synthetic tray.
- **Numbers on screen:** these come from `results/d3/test/metrics.json`:
  - 180 held-out synthetic cases;
  - 0 errors among automatic decisions;
  - a HUMAN_REVIEW rate of 0.361, shown as 36%.

  They sit next to the qualification "Research prototype • Synthetic-data evaluation": no
  real-world validation, and scores are not probabilities.
- **Architecture:** only components in `template.yaml`: HTTP API (GET /health, POST /inspect,
  throttled to 2 req/s with burst 5), Lambda (python3.13, x86_64, OpenCV 5 zip) and
  CloudWatch Logs (metadata only).

### Backend shown, and the deployed endpoint

The capture machine had no access to the deployed API (no URL configured, and the sandbox
network policy blocks AWS hosts). So:

- The app footage uses the **Local (in-process)** backend. The on-screen label and the app's
  own banner both say so. The HTTP API mode is **not** shown as working.
- The terminal in scene 05 is the real `python -m loopcheck.client` smoke test, run against
  `loopcheck.local_server`. That serves the **same Lambda handler** locally, and the terminal
  caption states this.

**To show your deployed smoke test instead:**
1. Run it yourself:
   ```bash
   uv run --frozen python -m loopcheck.client --url "$LOOPCHECK_API_URL" --case data/demo_cases/02_missing_part --expect DEFECT \
     > video/assets/api/smoke_deployed.txt
   ```
2. Check that the file contains no URL, account ID or other identifier (the client prints
   none).
3. Re-render.

When that file exists, the terminal uses it, shows the command as `--url $LOOPCHECK_API_URL`,
and changes its caption to "deployed API Gateway + Lambda (URL redacted)".

## Build

Requirements: the repository's uv environment (OpenCV 5, NumPy, Pillow) and `ffmpeg`/`ffprobe`
(tested with FFmpeg 6.1). No network access is needed at render time; fonts (Inter,
JetBrains Mono, OFL/Apache licences included) are vendored in `assets/fonts/`.

```bash
uv run --frozen python video/render_video.py            # full render: audio stems, film, QA report, contact sheet
uv run --frozen python video/render_video.py --preview  # 960x540 / 15 fps -> video/build/preview.mp4 (fast)
uv run --frozen python video/render_video.py --stills 5.5,24,44.8   # PNG frames -> video/build/stills/
uv run --frozen python video/render_video.py --remux    # audio, SFX or doc changes only: rebuild stems, re-mux
uv run --frozen python video/render_video.py --start 34 --end 53    # render a time range (silent picture in build/)
```

A full render takes about 100 s on 4 cores. It is deterministic: a clean rebuild, including
regenerating the pipeline imagery, produced identical decoded frames (2640 of 2640 identical
by framemd5). The pipeline imagery (`assets/pipeline/*/*.png`) is regenerated automatically
by the real service when missing.

Output specification (checked by `ffprobe` after every render; see `video/build/render_report.json`):

- **Video:** H.264 High, 1920×1080, 30/1 fps, 2640 frames, yuv420p, BT.709, CRF 16,
  `+faststart`.
- **Audio:** AAC 48 kHz stereo, 4 streams:

  | # | Track | Default | Loudness |
  |---|---|---|---|
  | 0 | Music + SFX (no narration) | yes | ≈ −27 LUFS, peaks ≈ −9 dBFS |
  | 1 | Music stem | | |
  | 2 | SFX stem | | |
  | 3 | Voice-over placeholder (silent) | | |

  Track 0 leaves headroom for a voice at about −16 LUFS.
- **Stems:** also written as WAV files in `video/audio/`.

## Recording and adding the voice-over

1. Read `voiceover_script.md`, which has pauses (`/`, `//`), pronunciation and delivery notes.
   The read is 186 words. Each scene's narration window is in `voiceover_timing.csv`.
2. Record one continuous take against the film: start at 00:00.5, 48 kHz WAV.
3. Export the final film:
   ```bash
   uv run --frozen python video/render_video.py --voiceover path/to/narration.wav
   ```
   This writes `video/loopcheck_demo_with_vo.mp4`. The music and effects bed is ducked under
   your voice with a side-chain compressor, then limited. The result is cut to exactly 88.0 s.
4. **If your read runs long or short in a scene:**
   - Change that scene's `vo` window (and, if needed, `start`/`end`) in `src/timeline.py`.
   - Re-render. The CSV and script regenerate from the same file.
   - Scene animations are keyed to absolute times inside each scene (`src/scenes.py`). Shift
     those too if a scene's length changes.
   - Sound-effect cues live in `timeline.SFX`. After changing only those, `--remux` is enough.

## Music and sound effects

- **Sound effects** (whooshes, scan, registration lock, clicks, decision hits) are original,
  synthesised in `src/audio.py`. Cue times and levels are in `timeline.SFX`.
- **Music** is an original synthesised pad (D minor colour) that follows `timeline.MUSIC_CUES`.
  It is deliberately restrained.
  - **To use a licensed track:** save it as `video/assets/music/music_override.wav` (48 kHz),
    then run `--remux`. The folder is Git-ignored, so licensed music is never committed.
  - The intended cue points are listed in `timeline.MUSIC_CUES`.

## Re-capturing the app

```bash
uv run --frozen --group demo streamlit run app/streamlit_app.py --server.headless true --server.address 127.0.0.1 \
  --server.port 8501 --browser.gatherUsageStats false --client.toolbarMode minimal --theme.base dark
uv run --frozen --group demo --with playwright==1.56.0 python video/capture/capture_app.py
```

- Playwright is pulled into a throwaway environment and is not a project dependency.
- On machines without the bundled Chromium, run `playwright install chromium` once.
- Captures are 3200×4000: a 1600×2000 viewport at device scale 2, so zooms stay sharp.
- `capture.json` records the control and panel coordinates the renderer uses for the pointer
  and the camera.

## Quality checks performed on this render

- Full render; `ffprobe`: 88.0 s, 1920×1080, 30 fps, H.264 High, 4 AAC streams named as above.
- `blackdetect`: no dark stretch over 0.5 s except the intended final fade (87.3–88.0 s).
  Scene changes are short dips.
- **Frames:** reviewed at two points per scene (`contact_sheet.jpg`) plus every scene boundary.
  Fixed during review:
  - an over-bright opening;
  - a header collision;
  - text overflowing chips and cards;
  - a pointer covering app text;
  - a title overlapping a moving panel;
  - an empty beat in scene 02;
  - the closing bracket colliding with the wordmark.
- **Audio** (analysed numerically, since there were no speakers in the capture environment):
  - all 33 effect cues have onsets at their cue times;
  - no clipped samples, and no discontinuities in the music;
  - section levels sit between −31 and −27 dBFS RMS;
  - the tail is silent.

  **Listen once on headphones and speakers before submission**; levels are easy to adjust in
  `timeline.SFX` and `audio.SFX_TRIM_DB`.
- The `--voiceover` export path was exercised with a test tone (not narration) and produced a
  single mixed track of exactly 88.0 s.
