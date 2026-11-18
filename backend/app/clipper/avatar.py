"""Animated round avatar ("webcam" character) rendered locally with Pillow.

The existing avatar asset is brought to life:
  * framed: soft drop shadow, a crisp rim on its own ring, and a blue glow that breathes
    gently when idle and lights up while it talks or reacts
  * always alive: breathing, slow head sway and small bob, natural random blinking
  * talking: mouth opens with the voice loudness (fast attack, slower release) and changes
    shape with the sound (round "oo" vs wide "ee/s" from the zero-crossing rate), with a
    small nod and lift while speaking
  * reacts: a quick pop + shake + glow flash at loud impact moments in the gameplay
  * enters with a short pop-in at the start of the Short
Frames are piped into ffmpeg and stored as a MOV with an alpha channel.
"""
from __future__ import annotations

import math
import os
import random
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import settings as cs

SIZE = 340          # avatar size on the Short (600 px asset is scaled to this)
CANVAS = 440        # room for glow, shadow, bounce and shake
SS = 4              # supersampling for drawn features
BASE = 600          # working size of the asset

# Feature positions in the 600x600 avatar.png (tuned for the provided avatar; override via env if you change it).
MOUTH = tuple(float(x) for x in os.environ.get("AVATAR_MOUTH", "292,274").split(","))
MOUTH_ENABLED = os.environ.get("AVATAR_MOUTH_ENABLED", "true").lower() in {"1", "true", "yes"}
EYES = [tuple(float(v) for v in e.split(",")) for e in os.environ.get("AVATAR_EYES", "242,202;328,197").split(";") if e]
EYE_W, EYE_H = 46, 24
BLINK_ENABLED = os.environ.get("AVATAR_BLINK_ENABLED", "true").lower() in {"1", "true", "yes"}
RING = tuple(float(x) for x in os.environ.get("AVATAR_RING", "292,302,270").split(","))  # cx, cy, r of the asset's circle

RIM_FROM, RIM_TO = -25, 205   # degrees (0 = right, 90 = bottom) where the ring isn't covered by hair

IMPACT_SECONDS = 0.4
ENTRANCE_SECONDS = 0.45


@lru_cache(maxsize=1)
def _base() -> Image.Image:
    im = Image.open(cs.AVATAR_PATH).convert("RGBA")
    if im.size != (BASE, BASE):
        im = im.resize((BASE, BASE), Image.LANCZOS)
    # crisp light rim on the asset's own ring makes it read as a framed webcam
    rim = Image.new("RGBA", (BASE * SS, BASE * SS), (0, 0, 0, 0))
    cx, cy, r = (v * SS for v in RING)
    # only along the visible ring (sides + bottom); the hair breaks out of the circle at the top
    ImageDraw.Draw(rim).arc((cx - r, cy - r, cx + r, cy + r), RIM_FROM, RIM_TO, fill=(200, 235, 255, 170), width=3 * SS)
    out = im.copy()
    out.alpha_composite(rim.resize((BASE, BASE), Image.LANCZOS))
    return out


def _draw_mouth(d: ImageDraw.ImageDraw, o: float, shape: int) -> None:
    cx, cy = MOUTH
    wf, hf = ((0.82, 1.15), (1.0, 1.0), (1.22, 0.78))[shape]
    w, h = (34 + 6 * o) * wf, (3 + 15 * o) * hf
    box = ((cx - w / 2) * SS, (cy - h / 2 + 2) * SS, (cx + w / 2) * SS, (cy + h / 2 + 2) * SS)
    d.ellipse(box, fill=(62, 22, 28, 255), outline=(190, 110, 102, 255), width=3 * SS)
    if o > 0.35:  # upper teeth
        d.chord(((cx - w / 2 + 5) * SS, (cy - h / 2 + 3) * SS, (cx + w / 2 - 5) * SS, (cy - h / 2 + 3 + h * 0.45) * SS),
                180, 360, fill=(235, 230, 224, 255))


def _eyelids(base: Image.Image, closed: float) -> Image.Image:
    """Eyelids made from the avatar's own skin just below each eye (real texture and shading),
    flipped and stretched over the eye with soft edges, plus a thin lash line.
    closed: 0.5 = half-closed (upper half of the eye covered), 1.0 = shut."""
    layer = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
    for ex, ey in EYES:
        x0, y0 = int(ex - EYE_W / 2), int(ey - EYE_H / 2)
        w, h = EYE_W, EYE_H + 2
        cover_h = h if closed >= 1 else max(4, int(h * 0.55))
        # skin patch under the eye (inside the glasses), mirrored so its shading runs top->bottom like a lid
        patch = base.crop((x0, y0 + h + 1, x0 + w, y0 + h + 15)).transpose(Image.FLIP_TOP_BOTTOM)
        patch = patch.resize((w, cover_h), Image.BICUBIC).convert("RGBA")
        mask = Image.new("L", (w * SS, cover_h * SS), 0)
        md = ImageDraw.Draw(mask)
        if closed >= 1:
            md.ellipse((0, 0, w * SS, cover_h * SS), fill=255)
        else:  # upper lid: dome down to a gentle curve
            md.chord((0, 0, w * SS, 2 * cover_h * SS), 180, 360, fill=255)
            md.rectangle((SS * 3, 0, (w - 3) * SS, cover_h * SS * 0.6), fill=255)
        mask = mask.resize((w, cover_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.8))
        patch.putalpha(mask)
        layer.alpha_composite(patch, (x0, y0))
        # lash line along the lower edge of the lid
        lash = Image.new("RGBA", (w * SS, 12 * SS), (0, 0, 0, 0))
        ImageDraw.Draw(lash).arc((2 * SS, -6 * SS, (w - 2) * SS, 9 * SS), 15, 165, fill=(45, 28, 26, 235), width=int(2.5 * SS))
        layer.alpha_composite(lash.resize((w, 12), Image.LANCZOS), (x0, y0 + cover_h - 9))
    return layer


@lru_cache(maxsize=64)
def _variant(mouth_level: int, mouth_shape: int, blink: int) -> Image.Image:
    """Avatar at 600 px with mouth level (0..6), mouth shape (0 round, 1 neutral, 2 wide) and blink (0..2)."""
    base = _base()
    if (mouth_level == 0 or not MOUTH_ENABLED) and (blink == 0 or not BLINK_ENABLED):
        return base
    out = base.copy()
    if mouth_level and MOUTH_ENABLED:
        layer = Image.new("RGBA", (BASE * SS, BASE * SS), (0, 0, 0, 0))
        _draw_mouth(ImageDraw.Draw(layer), mouth_level / 6, mouth_shape)
        out.alpha_composite(layer.resize((BASE, BASE), Image.LANCZOS))
    if blink and BLINK_ENABLED:
        out.alpha_composite(_eyelids(base, 0.5 if blink == 1 else 1.0))
    return out


def _circle_layer(radius: float, color: tuple, blur: float, offset: tuple[int, int] = (0, 0)) -> Image.Image:
    g = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    c = CANVAS / 2
    cy_shift = (RING[1] - BASE / 2) * SIZE / BASE
    x, y = c + offset[0], c + cy_shift + offset[1]
    ImageDraw.Draw(g).ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    return g.filter(ImageFilter.GaussianBlur(blur))


@lru_cache(maxsize=1)
def _shadow() -> Image.Image:
    return _circle_layer(RING[2] * SIZE / BASE, (0, 0, 0, 150), 12, (7, 13))


@lru_cache(maxsize=1)
def _glow() -> Image.Image:
    return _circle_layer(RING[2] * SIZE / BASE + 10, (50, 170, 255, 255), 16)


def _with_alpha(img: Image.Image, factor: float) -> Image.Image:
    factor = max(0.0, min(1.0, factor))
    if factor >= 0.999:
        return img
    out = img.copy()
    out.putalpha(out.getchannel("A").point(lambda a: int(a * factor)))
    return out


def _ease_out_back(x: float) -> float:
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def blink_schedule(duration: float, seed: int = 7) -> list[float]:
    """Natural blink start times: every 2.6-4.8 s, never in the first second."""
    rnd = random.Random(seed)
    t, out = 1.2 + rnd.random(), []
    while t < duration - 0.3:
        out.append(round(t, 2))
        t += 2.6 + rnd.random() * 2.2
    return out


def _blink_state(t: float, blinks: list[float]) -> int:
    for b in blinks:
        dt = t - b
        if 0 <= dt < 0.17:            # half, shut, shut, half (~5 frames at 30 fps)
            return 1 if dt < 0.04 or dt >= 0.12 else 2
    return 0


def frame(t: float, env: float, shape: int, speaking: bool, blink: int = 0, impact: float = 0.0,
          entrance: float = 1.0) -> Image.Image:
    """One RGBA frame. env 0..1 voice loudness, impact 0..1 (decaying), entrance 0..1 progress."""
    level = min(6, int(round(env * 7))) if speaking else 0
    img = _variant(level, shape if level else 1, blink)

    breathe = 0.012 * math.sin(2 * math.pi * 0.3 * t)
    talk = 0.04 * env if speaking else 0.0
    pop = 0.09 * impact
    enter = _ease_out_back(entrance) if entrance < 1 else 1.0
    scale = (1.0 + breathe + talk + pop) * (0.25 + 0.75 * enter)
    angle = 1.2 * math.sin(2 * math.pi * 0.17 * t) + (2.5 * env * math.sin(t * 8.5) if speaking else 0.0)
    dx = 9 * impact * math.sin(2 * math.pi * 19 * t)
    dy = 3 * math.sin(2 * math.pi * 0.3 * t + 1.0) - (6 * env if speaking else 0) + 6 * impact * math.cos(2 * math.pi * 23 * t)

    # Scale + rotate + move in ONE affine transform, from a pre-shrunk copy of the variant.
    src = _prescaled(level, shape if level else 1, blink)
    k = SIZE * scale / src.size[0]                      # output px per source px
    rad = math.radians(angle)
    cos_a, sin_a = math.cos(rad) / k, math.sin(rad) / k
    cx_out, cy_out = CANVAS / 2 + dx, CANVAS / 2 + dy
    cx_in = src.size[0] / 2
    # output (x, y) -> input: R(angle) applied to (x - c_out) / k, then + c_in (PIL rotates counter-clockwise)
    coeffs = (cos_a, -sin_a, cx_in - cos_a * cx_out + sin_a * cy_out,
              sin_a, cos_a, cx_in - sin_a * cx_out - cos_a * cy_out)
    av = src.transform((CANVAS, CANVAS), Image.AFFINE, coeffs, resample=Image.BILINEAR)

    fade = min(1.0, entrance * 2)
    glow_level = min(1.0, 0.28 + 0.06 * math.sin(2 * math.pi * 0.3 * t) + (0.7 * env if speaking else 0) + 0.8 * impact)
    canvas = _backdrop(round(fade * 20), round(glow_level * fade * 20)).copy()
    if dx or dy:  # shake / bob: move the backdrop with the avatar
        moved = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        moved.alpha_composite(canvas, (int(dx), int(dy)))
        canvas = moved
    canvas.alpha_composite(_with_alpha(av, fade))
    return canvas


@lru_cache(maxsize=256)
def _prescaled(level: int, shape: int, blink: int) -> Image.Image:
    """Variant pre-shrunk to ~1.15x the on-screen size (keeps quality for pops, much faster per frame)."""
    s = int(SIZE * 1.15)
    return _variant(level, shape, blink).resize((s, s), Image.LANCZOS)


@lru_cache(maxsize=512)
def _backdrop(fade_q: int, glow_q: int) -> Image.Image:
    """Shadow + glow at quantised opacity (0..20 steps), cached."""
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.alpha_composite(_with_alpha(_shadow(), fade_q / 20))
    canvas.alpha_composite(_with_alpha(_glow(), glow_q / 20))
    return canvas


def render_avatar_video(out: Path, duration: float, fps: int, envelope: list[float], speaking_mask: list[bool],
                        ffmpeg: str, zcr: list[float] | None = None, impacts: list | None = None,
                        seed: int = 7) -> None:
    """Render the avatar track. envelope/zcr/speaking_mask are per video frame. impacts: times in seconds,
    or (time, strength 0..1) pairs, e.g. 1.0 for a real impact and ~0.5 for a shouted line."""
    n = int(math.ceil(duration * fps))
    blinks = blink_schedule(duration, seed)
    impacts = sorted((i if isinstance(i, tuple) else (i, 1.0)) for i in (impacts or []))
    proc = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{CANVAS}x{CANVAS}", "-r", str(fps),
         "-i", "-", "-c:v", "qtrle", "-pix_fmt", "argb", str(out)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    smooth = 0.0
    try:
        for i in range(n):
            t = i / fps
            speaking = speaking_mask[i] if i < len(speaking_mask) else False
            raw = (envelope[i] if i < len(envelope) else 0.0) if speaking else 0.0
            smooth += (raw - smooth) * (0.65 if raw > smooth else 0.3)   # fast open, softer close
            z = zcr[i] if zcr and i < len(zcr) else 0.15
            shape = 2 if z > 0.22 else (0 if z < 0.08 else 1)
            impact = 0.0
            for ti, amp in impacts:
                if 0 <= t - ti < IMPACT_SECONDS:
                    impact = max(impact, amp * (1 - (t - ti) / IMPACT_SECONDS))
            entrance = min(1.0, t / ENTRANCE_SECONDS)
            img = frame(t, smooth, shape, speaking, _blink_state(t, blinks), impact, entrance)
            proc.stdin.write(img.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise RuntimeError(f"avatar encode failed: {err[-500:]}")
    except Exception:
        proc.kill()
        raise
