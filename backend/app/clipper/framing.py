"""Smart vertical framing: decide how the gameplay is placed in the 9:16 Short.

1. Scan the moment at low resolution and measure where things move (frame differences per
   column). Static overlays such as a minimap barely change, so they carry little weight, and a
   mild centre bias keeps third-person games on the player.
2. Choose a layout:
     * "full":  full-screen 9:16 crop (~32% of a 16:9 frame) that tracks the action, no bands.
       Used when the action is centred/concentrated and the source is at least 720p.
     * "focus": tall crop (1080x1350, ~45% of a 16:9 frame) that follows the action. Used when
       the motion is concentrated but not enough for full-screen.
     * "wide":  wider crop (1080x1000, ~62% of the frame) when the action is spread out.
     * "fill":  vertical/square sources simply fill the frame.
3. Build a smooth camera path for the crop window: it holds still, and only eases to a new
   position when the action has clearly moved (professional reframing, no jitter).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

W, H = 1080, 1920
SAMPLE_FPS = 4
SCAN_W = 160

LAYOUTS = {
    # name: (panel_w, panel_h, panel_y)
    "full": (1080, 1920, 0),
    "focus": (1080, 1350, 285),
    "wide": (1080, 1000, 400),
}
FULL_MIN_SCORE = 0.55    # share of motion inside the narrow full-screen window needed to go full-screen
FULL_MIN_SRC_H = 720     # below this the full-screen crop gets too soft
FOCUS_MIN_SCORE = 0.35   # below this, motion is dominated by the sides: use the wider crop
DEADBAND = 0.10          # fraction of source width the action must move before the camera follows
MAX_SPEED = 0.22         # fraction of source width per second the camera may pan
HOLD_SECONDS = 0.75      # the action must stay off-centre this long before the camera moves
GLOBAL_SPREAD = 0.55     # motion in more than this share of columns = the camera is moving, not the action
FOLLOW_RANGE = 0.14      # the crop centre stays within 0.5 +- this (player characters sit near the centre)
FOLLOW_RANGE_FULL = 0.06 # the narrow full-screen crop may only drift a little, or the player gets cut off


@dataclass
class Framing:
    layout: str
    panel_w: int
    panel_h: int
    panel_y: int
    crop_w: int = 0          # crop window in source pixels (0 = no crop)
    crop_h: int = 0
    x_expr: str = "0"        # ffmpeg expression for the crop x position over time t
    score: float = 0.0
    path: list[tuple[float, float]] = field(default_factory=list)  # (t, x) keyframes

    @property
    def panel_bottom(self) -> int:
        return self.panel_y + self.panel_h


def scan(ffmpeg: str, source: Path, start: float, duration: float, src_w: int, src_h: int
         ) -> tuple[list[list[float]], list[list[float]]]:
    """Per-sample motion energy: (columns over the full height, rows over the central 40% of the width)."""
    sh = max(2, int(round(SCAN_W * src_h / src_w / 2) * 2))
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-an",
         "-vf", f"fps={SAMPLE_FPS},scale={SCAN_W}:{sh}:flags=area,format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("motion scan failed: " + proc.stderr.decode(errors="replace")[-300:])
    frame_size = SCAN_W * sh
    data = proc.stdout
    frames = [data[i:i + frame_size] for i in range(0, len(data) - frame_size + 1, frame_size)]
    x0, x1 = int(SCAN_W * 0.3), int(SCAN_W * 0.7)
    cols_out, rows_out = [], []
    for prev, cur in zip(frames, frames[1:]):
        cols = [0.0] * SCAN_W
        rows = [0.0] * sh
        for y in range(sh):
            row = y * SCAN_W
            for x in range(SCAN_W):
                d = cur[row + x] - prev[row + x]
                if d > 6 or d < -6:          # ignore compression noise
                    ad = abs(d)
                    cols[x] += ad
                    if x0 <= x < x1:
                        rows[y] += ad
        cols_out.append(cols)
        rows_out.append(rows)
    return cols_out, rows_out


def scan_motion(ffmpeg: str, source: Path, start: float, duration: float, src_w: int, src_h: int) -> list[list[float]]:
    """Per-sample column motion energy (list of SCAN_W floats per sample)."""
    return scan(ffmpeg, source, start, duration, src_w, src_h)[0]


def _is_camera_motion(cols: list[float]) -> bool:
    peak = max(cols) if cols else 0
    return peak <= 0 or sum(1 for c in cols if c > 0.3 * peak) / len(cols) > GLOBAL_SPREAD


THIRD_PERSON_SHARE = 0.25   # share of samples with camera motion that marks third-person chase footage


def band_is_busy(cols_per_sample: list[list[float]], rows_per_sample: list[list[float]], band: tuple[float, float]) -> bool:
    """Would captions in this vertical band (fractions of the visible height) cover the subject/action?

    * camera moving for much of the clip (third-person chase): the lower half holds the player/car;
    * camera mostly still: a band that moves much MORE than the rest holds the action."""
    if not rows_per_sample:
        return False
    moving_share = sum(1 for c in cols_per_sample if _is_camera_motion(c)) / max(1, len(cols_per_sample))
    if moving_share >= THIRD_PERSON_SHARE and band[0] >= 0.45:
        # Camera moving for much of the clip = third-person chase/driving: the player or their car travels
        # with the camera in the lower half of the frame. (Tested: motion statistics can't reliably find
        # a car that drifts around the frame, so the lower band is treated as taken.)
        return True
    moving = [i for i, c in enumerate(cols_per_sample) if _is_camera_motion(c)]
    still = [i for i in range(len(cols_per_sample)) if i not in set(moving)]
    use_moving = len(moving) >= 0.25 * len(cols_per_sample)
    idx = moving if use_moving else still
    ratios = []
    for i in idx:
        r = rows_per_sample[i]
        n = len(r)
        a, b = max(0, int(band[0] * n)), min(n, max(int(band[0] * n) + 1, int(band[1] * n)))
        total = sum(r) / n
        if total <= 0:
            continue
        ratios.append((sum(r[a:b]) / (b - a)) / total)
    if not ratios:
        return False
    avg = sum(ratios) / len(ratios)
    return avg < 0.6 if use_moving else avg > 1.5


def _best_window(cols: list[float], win: int, prior: list[float]) -> tuple[int, float]:
    """Left edge of the window (in scan columns) with the most centre-weighted motion, and its raw share."""
    weighted = [c * p for c, p in zip(cols, prior)]
    total_raw = sum(cols)
    s = sum(weighted[:win])
    best, best_i = s, 0
    for i in range(1, len(cols) - win + 1):
        s += weighted[i + win - 1] - weighted[i - 1]
        if s > best:
            best, best_i = s, i
    share = sum(cols[best_i:best_i + win]) / total_raw if total_raw > 0 else 1.0
    return best_i, share


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2]


def camera_path(targets: list[float | None], times: list[float], centre: float) -> list[tuple[float, float]]:
    """Turn noisy per-sample targets (normalised 0..1 window centre; None = no opinion, hold)
    into a calm camera path."""
    if not targets:
        return [(0.0, centre)]
    k = 5   # median over ~2.75 s; a target needs support from most of the window to count
    smooth: list[float | None] = []
    for i in range(len(targets)):
        window = targets[max(0, i - k):i + k + 1]
        vals = [v for v in window if v is not None]
        smooth.append(_median(vals) if len(vals) > len(window) // 2 else None)
    first = next((v for v in smooth if v is not None), centre)
    pos = first if abs(first - centre) > DEADBAND and smooth[0] is not None else centre
    path = [(0.0, pos)]
    off_since = None
    moving_to = None          # once the camera moves, it settles on the target (hysteresis)
    dt = 1.0 / SAMPLE_FPS
    for t, target in zip(times, smooth):
        if target is None:
            off_since = None if moving_to is None else off_since
            target = moving_to if moving_to is not None else pos
        if moving_to is None:
            if abs(target - pos) > DEADBAND:
                off_since = off_since if off_since is not None else t
                if t - off_since >= HOLD_SECONDS:
                    moving_to = target
            else:
                off_since = None
        else:
            moving_to = target if abs(target - moving_to) > DEADBAND / 2 else moving_to
        if moving_to is not None:
            if path[-1][0] < t - dt:
                path.append((t - dt, pos))  # hold until now, then start moving
            # ease-out: fast at first, slowing down as it arrives; capped speed
            step = max(-MAX_SPEED * dt, min(MAX_SPEED * dt, 0.45 * (moving_to - pos)))
            pos += step
            path.append((t, pos))
            if abs(moving_to - pos) < 0.01:
                moving_to, off_since = None, None
    path.append((times[-1] + dt, pos))
    # drop points that lie on a straight line (keeps the ffmpeg expression short)
    simplified = [path[0]]
    for i in range(1, len(path) - 1):
        (t0, x0), (t1, x1), (t2, x2) = simplified[-1], path[i], path[i + 1]
        if t2 == t0 or abs(x0 + (x2 - x0) * (t1 - t0) / (t2 - t0) - x1) > 0.002:
            simplified.append(path[i])
    simplified.append(path[-1])
    return simplified


def path_expr(path: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    """Piecewise-linear ffmpeg expression for crop x (pixels) as a function of t."""
    max_x = src_w - crop_w

    def px(c: float) -> float:
        return round(min(max_x, max(0.0, c * src_w - crop_w / 2)), 1)

    if len(path) == 1 or all(abs(px(x) - px(path[0][1])) < 0.5 for _, x in path):
        return str(px(path[0][1]))
    terms = []
    for (t0, x0), (t1, x1) in zip(path, path[1:]):
        a, b = px(x0), px(x1)
        if t1 <= t0:
            continue
        seg = f"{a}" if a == b else f"({a}+({b - a:.1f})*(t-{t0:.3f})/{t1 - t0:.3f})"
        # half-open intervals [t0, t1): exactly one term is active at any time
        terms.append(f"gte(t,{t0:.3f})*lt(t,{t1:.3f})*{seg}")
    terms.append(f"gte(t,{path[-1][0]:.3f})*{px(path[-1][1])}")
    return f"min({max_x},max(0,{'+'.join(terms)}))"


def plan(cols_per_sample: list[list[float]], src_w: int, src_h: int) -> Framing:
    aspect = src_w / src_h
    if aspect < 1.05:  # vertical or square source: fill the frame
        return Framing("fill", W, H, 0)

    def crop_for(layout: str) -> tuple[int, int]:
        pw, ph, _ = LAYOUTS[layout]
        cw = int(round(src_h * pw / ph / 2) * 2)
        return min(cw, src_w), src_h

    n = len(cols_per_sample[0]) if cols_per_sample else SCAN_W
    prior = [0.55 + 0.45 * (1 - abs((i + 0.5) / n - 0.5) * 2) for i in range(n)]

    def analyse(layout: str) -> tuple[float, list[float | None]]:
        """Score = how well the crop window contains localized action; targets = where to point it.

        In third-person gameplay the player stays near the centre while the scenery streams past,
        so motion spread over the whole frame is *camera* motion and is ignored (the crop holds).
        Only localized action (a crash, an NPC, an explosion) pulls the crop, within FOLLOW_RANGE."""
        cw, _ = crop_for(layout)
        win = max(1, int(round(n * cw / src_w)))
        energies = [sum(c) for c in cols_per_sample]
        mean_energy = sum(energies) / len(energies) if energies else 0
        targets: list[float | None] = []
        local_shares, local_w = [], []
        for cols, energy in zip(cols_per_sample, energies):
            peak = max(cols) if cols else 0
            spread = sum(1 for c in cols if c > 0.3 * peak) / len(cols) if peak > 0 else 1.0
            if energy < 0.15 * mean_energy or spread > GLOBAL_SPREAD:
                targets.append(None)          # quiet or camera motion: no opinion, the camera holds
                continue
            i, _ = _best_window(cols, win, prior)
            rng = FOLLOW_RANGE_FULL if layout == "full" else FOLLOW_RANGE
            target = min(0.5 + rng, max(0.5 - rng, (i + win / 2) / n))
            targets.append(target)
            # score the window where the crop can actually go (the follow range may keep it from the action)
            left = max(0, min(n - win, int(round(target * n - win / 2))))
            local_shares.append(sum(cols[left:left + win]) / energy if energy > 0 else 1.0)
            local_w.append(energy)
        if len(local_w) < 0.2 * max(1, len(energies)):
            score = 1.0                       # mostly camera motion: a centred crop keeps the player in view
        else:
            score = sum(s * w for s, w in zip(local_shares, local_w)) / sum(local_w)
        return score, targets

    full_score, full_targets = analyse("full") if cols_per_sample else (1.0, [])
    focus_score, focus_targets = analyse("focus") if cols_per_sample else (1.0, [])
    if src_h >= FULL_MIN_SRC_H and full_score >= FULL_MIN_SCORE:
        layout, score, targets = "full", full_score, full_targets
    elif focus_score >= FOCUS_MIN_SCORE:
        layout, score, targets = "focus", focus_score, focus_targets
    else:
        layout = "wide"
        score, targets = analyse("wide")
    cw, ch = crop_for(layout)
    times = [(i + 1) / SAMPLE_FPS for i in range(len(targets))]
    path = camera_path(targets, times, 0.5)
    pw, ph, py = LAYOUTS[layout]
    return Framing(layout, pw, ph, py, cw, ch, path_expr(path, src_w, cw), round(score, 3), path)
