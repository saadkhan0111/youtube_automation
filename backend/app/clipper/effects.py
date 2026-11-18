"""Context-aware editing effects.

Nothing here is random: every effect is triggered by a signal in the clip.
  * impacts: sudden loudness jumps in the clip (10 measurements/s), well above the previous
    1.5 s. At most MAX_IMPACTS per clip, at least MIN_GAP apart, not in the first second.
  * each impact: punch zoom, camera shake, short flash, boom sound, avatar reaction
  * strongest impact: slow push-in during the 2 s before it; optional slow motion (once, only
    if it is very strong, never during the avatar's voice or at the very end)
  * always: whip zoom + light flash + whoosh at the start (with the hook card), pop when the
    card lands, gentle push-in while the outro is spoken, fade at the end

Slow motion stretches the timeline, so everything is placed on the OUTPUT timeline through
TimeMap (captions, voices, avatar, crop path, effects).
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FPS = 30
RATE = 10                 # loudness measurements per second
MAX_IMPACTS = 3
MIN_GAP = 4.0             # seconds between impacts
IMPACT_LU = 7.0           # jump above the recent median that counts as an impact
IMPACT_FLOOR = -32.0      # ignore jumps in near-silence (LUFS)
SLOWMO_LU = 9.0          # a real impact must be this strong for slow motion (also: once per clip, not during voice)
SLOWMO_LEN = 0.8          # seconds of source played at half speed
SLOWMO_FACTOR = 2.0
SLOWMO_ENABLED = os.environ.get("CLIP_SLOWMO", "true").lower() in {"1", "true", "yes"}


@dataclass
class Impact:
    t: float                 # input (source clip) time
    strength: float          # LU above the recent median
    kind: str = "impact"     # impact (crash/shot/explosion) | emphasis (shouted line) | transition (scene cut)


EXCLAIM = re.compile(r"(!|\b(fuck\w*|shit\w*|damn|wow|whoa|holy|omg|boom|crash\w*|run|go|help|no+|yes+|what)\b)",
                     re.IGNORECASE)


def scene_cuts(ffmpeg: str, source: Path, start: float, duration: float, threshold: float = 0.35) -> list[float]:
    """Times (clip-relative) of hard cuts, from ffmpeg's scene-change score."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-an",
         "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return [round(float(m.group(1)), 2) for m in re.finditer(r"pts_time:([\d.]+)", proc.stderr)]


def classify(impacts: list[Impact], words: list[dict], cuts: list[float] | None = None,
             window: float = 0.35) -> list[Impact]:
    """Tell real impacts from cuts and speech.
    words: clip-relative [{start, end, word}]. A loud jump on a hard cut is a transition; with no
    speech around it, an impact; on a shouted exclamation, emphasis; ordinary dialogue is ignored."""
    out = []
    for i in impacts:
        if any(abs(c - i.t) <= 0.6 for c in cuts or []):
            out.append(Impact(i.t, i.strength, "transition"))
            continue
        near = [w["word"] for w in words if w["end"] > i.t - window and w["start"] < i.t + window]
        if not near:
            out.append(Impact(i.t, i.strength, "impact"))
        elif EXCLAIM.search(" ".join(near)):
            out.append(Impact(i.t, i.strength, "emphasis"))
    return out


@dataclass
class TimeMap:
    """Input clip time -> output time (identity unless a slow-motion window exists)."""
    slow: tuple[float, float] | None = None
    factor: float = SLOWMO_FACTOR

    def out(self, t: float) -> float:
        if not self.slow:
            return t
        a, b = self.slow
        if t <= a:
            return t
        if t <= b:
            return a + (t - a) * self.factor
        return t + (b - a) * (self.factor - 1)

    def duration(self, d: float) -> float:
        return self.out(d)


@dataclass
class EffectPlan:
    impacts: list[Impact] = field(default_factory=list)
    timemap: TimeMap = field(default_factory=TimeMap)
    buildup_to: float | None = None        # output time of the strongest impact (push-in before it)

    def impact_times_out(self) -> list[float]:
        """Real impacts only (shake, flash, boom, avatar reaction)."""
        return [self.timemap.out(i.t) for i in self.impacts if i.kind == "impact"]

    def transition_times_out(self) -> list[float]:
        """Loud scene cuts: whoosh + light flash only."""
        return [self.timemap.out(i.t) for i in self.impacts if i.kind == "transition"]

    def emphasis_times_out(self) -> list[float]:
        """Shouted exclamations: a small punch-in only."""
        return [self.timemap.out(i.t) for i in self.impacts if i.kind == "emphasis"]


# --------------------------------------------------------------------------- detection
def clip_loudness(ffmpeg: str, source: Path, start: float, duration: float) -> list[float]:
    """Momentary loudness (LUFS) every 0.1 s for the clip only."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-v", "verbose", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
         "-i", str(source), "-vn", "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    pat = re.compile(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+|-inf)")
    vals: list[float] = []
    for line in proc.stderr.splitlines():
        m = pat.search(line)
        if m:
            v = m.group(2)
            vals.append(-70.0 if v == "-inf" else max(-70.0, float(v)))
    return vals


def detect_impacts(curve: list[float], duration: float) -> list[Impact]:
    """Sudden loudness jumps: value vs. the median of the 1.5 s before it."""
    cands = []
    for i in range(len(curve)):
        t = i / RATE
        if t < 1.0 or t > duration - 0.5:
            continue
        prev = curve[max(0, i - 15):max(0, i - 2)]
        if len(prev) < 5:
            continue
        base = sorted(prev)[len(prev) // 2]
        jump = curve[i] - base
        if curve[i] >= IMPACT_FLOOR and jump >= IMPACT_LU:
            # keep only the local maximum of a burst
            window = curve[max(0, i - 3):i + 4]
            if curve[i] >= max(window):
                cands.append(Impact(round(t, 2), round(jump, 1)))
    picked: list[Impact] = []
    for c in sorted(cands, key=lambda c: c.strength, reverse=True):
        if all(abs(c.t - p.t) >= MIN_GAP for p in picked):
            picked.append(c)
        if len(picked) >= MAX_IMPACTS:
            break
    return sorted(picked, key=lambda c: c.t)


def plan(impacts: list[Impact], duration: float, protected: list[tuple[float, float]], allow_slowmo: bool = SLOWMO_ENABLED) -> EffectPlan:
    """Choose effects. `protected` = input-time windows where slow motion must not happen (avatar voice)."""
    ep = EffectPlan(impacts=list(impacts))
    real = [i for i in impacts if i.kind == "impact"]
    if not real:
        return ep
    best = max(real, key=lambda i: i.strength)
    a, b = best.t - 0.15, best.t - 0.15 + SLOWMO_LEN
    ok = (allow_slowmo and best.strength >= SLOWMO_LU and duration >= 12 and a >= 4.0 and b <= duration - 3.5
          and not any(a < pe and b > ps for ps, pe in protected))
    if ok:
        ep.timemap = TimeMap((round(a, 3), round(b, 3)))
    ep.buildup_to = ep.timemap.out(best.t) if best.t >= 3.0 else None
    return ep


# --------------------------------------------------------------------------- expressions (output timeline)
def zoom_expr(ep: EffectPlan, out_duration: float, outro: tuple[float, float] | None) -> str:
    """zoompan zoom factor as a function of the output frame number `on`."""
    terms = ["1", "0.12*max(0,1-on/12)"]                       # whip-zoom intro
    for t in ep.impact_times_out():                             # punch on every impact, quick decay
        f = int(t * FPS)
        terms.append(f"0.13*gte(on,{f})*exp(-(on-{f})/7)")
    for t in ep.emphasis_times_out():                           # small punch on shouted exclamations
        f = int(t * FPS)
        terms.append(f"0.06*gte(on,{f})*exp(-(on-{f})/9)")
    if ep.buildup_to:                                           # slow push-in before the main impact
        f1 = int(ep.buildup_to * FPS)
        f0 = max(0, f1 - 2 * FPS)
        # ramps up to the impact, then eases back down (no jump right after the hit)
        terms.append(f"0.06*(between(on,{f0},{f1})*(on-{f0})/{max(1, f1 - f0)}+gt(on,{f1})*exp(-(on-{f1})/8))")
    if outro:                                                   # gentle push-in while the outro is spoken
        f0, f1 = int(outro[0] * FPS), int(min(outro[1], out_duration) * FPS)
        terms.append(f"0.05*between(on,{f0},{f1})*(on-{f0})/{max(1, f1 - f0)}")
    return "+".join(terms)


def shake_terms(ep: EffectPlan, amp: float = 16.0) -> tuple[str, str]:
    """Extra x/y offsets (pixels) for zoompan: a short decaying shake after each impact."""
    xs, ys = [], []
    for t in ep.impact_times_out():
        f = int(t * FPS)
        gate = f"between(on,{f},{f + 12})*exp(-(on-{f})/5)"
        xs.append(f"{amp}*{gate}*sin((on-{f})*2.3)")
        ys.append(f"{amp * 0.7}*{gate}*cos((on-{f})*2.9)")
    return ("+".join(xs) or "0"), ("+".join(ys) or "0")


def brightness_expr(ep: EffectPlan) -> str:
    """eq brightness over time t: flash-in at the start, short flash on each impact."""
    terms = ["0.16*max(0,1-t/0.12)"]
    for t in ep.impact_times_out():
        terms.append(f"0.20*gte(t,{t:.3f})*max(0,1-(t-{t:.3f})/0.09)")
    for t in ep.transition_times_out():
        terms.append(f"0.12*gte(t,{t:.3f})*max(0,1-(t-{t:.3f})/0.12)")
    return "+".join(terms)


# --------------------------------------------------------------------------- sound effects
SFX_VERSION = 2   # bump when the specs change so cached files are regenerated
SFX_SPECS = {
    # whoosh: band-limited pink noise swelling in and out
    "whoosh": "anoisesrc=color=pink:d=0.55:a=0.9,highpass=f=350,lowpass=f=4200,aphaser=type=t:speed=2,"
              "afade=t=in:d=0.28:curve=exp,afade=t=out:st=0.28:d=0.27:curve=exp,volume=9,alimiter=limit=0.7",
    # boom: deep sine thump + short noise transient
    "boom": "sine=f=52:d=0.7,volume=1.6,afade=t=out:d=0.7:curve=exp[s];"
            "anoisesrc=color=brown:d=0.12:a=0.9,lowpass=f=900,afade=t=out:d=0.12[n];[s][n]amix=inputs=2:normalize=0,"
            "acompressor=threshold=0.5:ratio=4,volume=2.2,alimiter=limit=0.85",
    # pop: short bright blip for the hook card landing
    "pop": "sine=f=880:d=0.07,afade=t=out:d=0.07:curve=exp,volume=8,alimiter=limit=0.6",
}


def ensure_sfx(ffmpeg: str, out_dir: Path) -> dict[str, Path]:
    """Synthesize the sound effects once (cached on disk)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, graph in SFX_SPECS.items():
        p = out_dir / f"{name}_v{SFX_VERSION}.wav"
        if not p.exists():
            tmp = p.with_suffix(".tmp.wav")
            subprocess.run([ffmpeg, "-y", "-v", "error", "-filter_complex", graph + ",aresample=48000,aformat=channel_layouts=stereo",
                            "-t", "1", str(tmp)], check=True)
            tmp.replace(p)
        paths[name] = p
    return paths


def sfx_cues(ep: EffectPlan, card_end: float | None) -> list[tuple[str, float, float]]:
    """(effect, output time, volume)."""
    cues = [("whoosh", 0.0, 0.55), ("pop", 0.14, 0.6)]
    if card_end:
        cues.append(("whoosh", max(0.0, card_end - 0.3), 0.35))
    for t in ep.impact_times_out():
        cues.append(("boom", max(0.0, t - 0.02), 0.9))
    for t in ep.transition_times_out():
        cues.append(("whoosh", max(0.0, t - 0.3), 0.45))
    return cues
