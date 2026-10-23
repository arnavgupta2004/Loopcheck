"""Original, deterministic audio for the LoopCheck film: music bed, sound effects, VO placeholder.

Everything is synthesised here with numpy (fixed seeds), so the render never downloads or
depends on third-party audio. Stems are 48 kHz / 16-bit stereo WAV:
  music.wav      restrained pad following timeline.MUSIC_CUES (replace with a licensed track if preferred)
  sfx.wav        transition / registration / click / decision cues from timeline.SFX
  voiceover.wav  silent placeholder of the full duration (replace with the recorded narration)
  mix_no_vo.wav  music + sfx, headroom left for the voice-over
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

from timeline import DURATION, SFX

SR = 48000
N = int(round(DURATION * SR))
SFX_TRIM_DB = 7.0  # global trim applied to the per-cue gains in timeline.SFX


def db(x: float) -> float:
    return 10 ** (x / 20)


def write_wav(path: Path, x: np.ndarray) -> None:
    x = np.atleast_2d(x)
    if x.shape[0] in (1, 2) and x.shape[0] < x.shape[1]:
        x = x.T
    if x.shape[1] == 1:
        x = np.repeat(x, 2, 1)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        n, ch, sw = w.getnframes(), w.getnchannels(), w.getsampwidth()
        data = np.frombuffer(w.readframes(n), "<i2").astype(np.float32) / 32767
    return data.reshape(-1, ch)


# ----------------------------------------------------------------------------- dsp helpers
def onepole_lp(x: np.ndarray, cutoff_hz) -> np.ndarray:
    """One-pole low-pass; cutoff may be an array (time-varying)."""
    c = np.broadcast_to(np.asarray(cutoff_hz, np.float64), x.shape)
    a = np.exp(-2 * np.pi * c / SR)
    y = np.empty_like(x, dtype=np.float64)
    acc = 0.0
    for i in range(len(x)):
        acc = (1 - a[i]) * x[i] + a[i] * acc
        y[i] = acc
    return y


def lp_fft(x: np.ndarray, cutoff: float, order: int = 1) -> np.ndarray:
    """Zero-phase low-pass with a one-pole-like magnitude response (fast for long signals)."""
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(X / (1 + (f / cutoff) ** 2) ** (order / 2), len(x))


def bandpass_fft(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    g = np.exp(-0.5 * ((np.log(np.maximum(f, 1)) - np.log(math.sqrt(lo * hi))) / (0.5 * math.log(hi / lo))) ** 2)
    return np.fft.irfft(X * g, len(x))


def env_adsr(n: int, a: float, r: float) -> np.ndarray:
    t = np.arange(n) / SR
    e = np.minimum(1, t / max(a, 1e-4))
    rel = np.clip((n / SR - t) / max(r, 1e-4), 0, 1)
    return e * rel


def pan(x: np.ndarray, p) -> np.ndarray:
    """p in [-1, 1] (scalar or array), equal-power."""
    p = np.broadcast_to(np.asarray(p, np.float64), x.shape)
    ang = (p + 1) * np.pi / 4
    return np.stack([x * np.cos(ang), x * np.sin(ang)], 1)


# ----------------------------------------------------------------------------- sound effects
def sfx(kind: str, rng: np.random.Generator) -> np.ndarray:
    if kind in ("whoosh", "whoosh_soft", "swish"):
        dur = {"whoosh": 0.95, "whoosh_soft": 0.8, "swish": 0.45}[kind]
        n = int(dur * SR)
        t = np.arange(n) / SR
        x = rng.standard_normal(n)
        shape = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** (2.2 if kind != "swish" else 1.4)
        cut = 300 + 3200 * shape if kind != "swish" else 900 + 5200 * shape
        y = onepole_lp(onepole_lp(x, cut), cut) * shape
        y -= onepole_lp(y, 120)
        return pan(y / (np.abs(y).max() + 1e-9), np.linspace(-0.5, 0.5, n))
    if kind == "scan":
        n = int(1.5 * SR)
        t = np.arange(n) / SR
        y = bandpass_fft(rng.standard_normal(n), 1800, 5200) * np.sin(np.pi * t / 1.5) ** 2
        y += 0.15 * np.sin(2 * np.pi * 3100 * t) * np.sin(np.pi * t / 1.5) ** 4
        return pan(y / np.abs(y).max(), np.linspace(-0.85, 0.85, n))
    if kind in ("ping", "ping_low"):
        f0 = 1318.5 if kind == "ping" else 659.3
        n = int(1.4 * SR)
        t = np.arange(n) / SR
        y = (np.sin(2 * np.pi * f0 * t) + 0.25 * np.sin(2 * np.pi * f0 * 2.01 * t)) * np.exp(-t * 4.5) * np.minimum(1, t / 0.004)
        return pan(y / np.abs(y).max(), 0.15)
    if kind in ("hit", "hit_soft"):
        dur = 1.6 if kind == "hit" else 1.0
        n = int(dur * SR)
        t = np.arange(n) / SR
        f = 38 + 60 * np.exp(-t * 18)
        y = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * (2.6 if kind == "hit" else 4.5)) * np.minimum(1, t / 0.003)
        air = onepole_lp(rng.standard_normal(n), 2400) * np.exp(-t * 9) * 0.25
        y = y + air
        if kind == "hit":
            y += 0.18 * np.sin(2 * np.pi * 293.7 * t) * np.exp(-t * 1.8) * np.minimum(1, t / 0.02)
        return pan(y / np.abs(y).max(), 0)
    if kind in ("tick", "click"):
        n = int(0.06 * SR)
        t = np.arange(n) / SR
        f0 = 2400 if kind == "tick" else 1700
        y = np.sin(2 * np.pi * f0 * t) * np.exp(-t * 160) + 0.6 * rng.standard_normal(n) * np.exp(-t * 900)
        if kind == "click":
            y -= onepole_lp(y, 600)
        return pan(y / np.abs(y).max(), 0.2 if kind == "click" else -0.1)
    if kind == "lock":
        a, b = sfx("tick", rng), sfx("tick", rng)
        out = np.zeros((int(0.9 * SR), 2))
        out[: len(a)] += a
        o = int(0.07 * SR)
        out[o:o + len(b)] += b * 0.9
        p = sfx("ping", rng)[: len(out) - o] * 0.35
        out[o:o + len(p)] += p
        return out / np.abs(out).max()
    if kind == "swell":
        n = int(3.2 * SR)
        t = np.arange(n) / SR
        e = np.sin(np.pi * np.clip(t / 3.2, 0, 1)) ** 2
        y = 0.7 * np.sin(2 * np.pi * 55 * t) + onepole_lp(rng.standard_normal(n), 300 + 900 * e) * 0.6
        return pan((y * e) / np.abs(y * e).max(), 0)
    raise ValueError(kind)


def render_sfx() -> np.ndarray:
    out = np.zeros((N, 2))
    for i, (t0, kind, gain) in enumerate(SFX):
        x = sfx(kind, np.random.default_rng(1000 + i)) * db(gain + SFX_TRIM_DB)
        s = int(round(t0 * SR))
        e = min(N, s + len(x))
        out[s:e] += x[: e - s]
    return out


# ----------------------------------------------------------------------------- music bed
NOTE = {n: i for i, n in enumerate(["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"])}


def hz(name: str) -> float:
    pitch, octave = name[:-1], int(name[-1])
    return 440.0 * 2 ** ((NOTE[pitch] + 12 * (octave + 1) - 69) / 12)


CHORDS = [  # (start s, notes) — D minor colour, resolving to D at the end
    (0.0, ["D2", "A2", "F3", "C4", "E4"]), (8.0, ["A#1", "F2", "D3", "A3", "C4"]), (14.0, ["F2", "C3", "A3", "E4"]),
    (20.0, ["D2", "A2", "F3", "C4", "E4"]), (27.0, ["G1", "D2", "A#2", "F3", "A3"]), (34.0, ["A#1", "F2", "D3", "A3", "C4"]),
    (41.0, ["F2", "C3", "A3", "E4"]), (47.0, ["D2", "A2", "F3", "C4", "E4"]), (53.0, ["G1", "D2", "A#2", "F3", "A3"]),
    (60.0, ["A#1", "F2", "D3", "A3", "C4"]), (67.0, ["D2", "A2", "F3", "C4"]), (74.0, ["A1", "E2", "C3", "G3"]),
    (80.0, ["D2", "A2", "D3", "F#3", "A3", "E4"]),
]
LEVEL = [(0.0, 0.0), (3.0, 0.55), (8.0, 0.8), (19.0, 0.8), (21.0, 0.6), (33.0, 0.6), (35.0, 0.72), (52.0, 0.72),
         (54.0, 0.62), (66.0, 0.62), (68.0, 0.48), (79.0, 0.48), (81.0, 0.85), (85.5, 0.75), (87.6, 0.0), (DURATION, 0.0)]


def render_music() -> np.ndarray:
    t_all = np.arange(N) / SR
    out = np.zeros((N, 2))
    rng = np.random.default_rng(7)
    for i, (t0, notes) in enumerate(CHORDS):
        t1 = CHORDS[i + 1][0] if i + 1 < len(CHORDS) else DURATION
        s, e = int(t0 * SR), min(N, int((t1 + 2.5) * SR))
        t = (np.arange(e - s)) / SR
        seg = np.zeros((e - s, 2))
        for j, nme in enumerate(notes):
            f = hz(nme)
            v = np.zeros(e - s)
            for det in (-0.12, 0.0, 0.13):  # gentle chorus, soft triangle-like partials
                ph = rng.uniform(0, 2 * np.pi)
                ff = f * 2 ** (det / 12)
                v += np.sin(2 * np.pi * ff * t + ph) + np.sin(2 * np.pi * 3 * ff * t + ph) / 9 + np.sin(2 * np.pi * 5 * ff * t + ph) / 30
            v *= (1 / (1 + j * 0.35)) * (0.75 + 0.25 * np.sin(2 * np.pi * (0.07 + 0.013 * j) * t + j))
            seg += pan(v, (-0.6 + 1.2 * j / max(len(notes) - 1, 1)) * 0.6)
        seg *= env_adsr(e - s, 2.2, 2.5)[:, None]
        out[s:e] += seg
    noise = np.random.default_rng(11).standard_normal(N)
    air = lp_fft(noise, 1200) - lp_fft(noise, 200)
    out += np.stack([air, np.roll(air, 2400)], 1) * 0.02
    lvl = np.interp(t_all, [a for a, _ in LEVEL], [b for _, b in LEVEL])
    out *= lvl[:, None]
    # soft low-pass to keep the pad warm and out of the way of speech
    for ch in range(2):
        out[:, ch] = lp_fft(out[:, ch], 2200)
    return out / (np.abs(out).max() + 1e-9) * db(-13)


MUSIC_OVERRIDE = Path(__file__).resolve().parents[1] / "assets" / "music" / "music_override.wav"


def load_music_override() -> np.ndarray | None:
    """A licensed track dropped in as assets/music/music_override.wav (48 kHz WAV) replaces the pad."""
    if not MUSIC_OVERRIDE.exists():
        return None
    with wave.open(str(MUSIC_OVERRIDE), "rb") as w:
        if w.getframerate() != SR:
            raise SystemExit(f"{MUSIC_OVERRIDE} must be {SR} Hz (convert with: ffmpeg -i in.mp3 -ar 48000 -ac 2 music_override.wav)")
    x = read_wav(MUSIC_OVERRIDE).astype(np.float64)
    x = np.repeat(x, 2, 1) if x.shape[1] == 1 else x[:, :2]
    out = np.zeros((N, 2))
    out[: min(N, len(x))] = x[:N]
    fade = np.clip((DURATION - np.arange(N) / SR - 0.4) / 2.0, 0, 1)  # fade out over the last ~2.4 s
    return out * fade[:, None]


def build(audio_dir: Path) -> dict:
    music = load_music_override()
    if music is None:
        music = render_music()
    fx = render_sfx()
    mix = music + fx
    peak = np.abs(mix).max()
    if peak > db(-1):
        mix *= db(-1) / peak
    write_wav(audio_dir / "music.wav", music)
    write_wav(audio_dir / "sfx.wav", fx)
    write_wav(audio_dir / "mix_no_vo.wav", mix)
    write_wav(audio_dir / "voiceover.wav", np.zeros((N, 2)))
    rms = lambda x: 20 * math.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)  # noqa: E731
    return {"music_rms_dbfs": round(rms(music), 1), "sfx_peak_dbfs": round(20 * math.log10(np.abs(fx).max() + 1e-12), 1),
            "mix_peak_dbfs": round(20 * math.log10(np.abs(mix).max() + 1e-12), 1), "seconds": DURATION}
