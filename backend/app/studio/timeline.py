"""Edit decision list for a Studio video: where every scene, shot, transition, sound effect, caption and graphic goes.

Pure functions (no ffmpeg) so the edit can be tested and inspected. Times are kept in whole frames to avoid
drift when dozens of shots are joined.

Shot timing: a scene lasts exactly as long as its recorded narration (+ its pause, + a short breath before reveals);
its shots split that time by their storyboard `share`. Transitions overlap the two shots around a boundary
(half before, half after), so the boundary itself never moves.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..clipper import captions as cap
from . import styles

FPS = 30
TAIL = 0.8                     # hold after the last word
MIN_SHOT_FRAMES = 12           # 0.4 s
PAUSE_BEFORE_REVEAL = 0.15

# storyboard transition -> (ffmpeg xfade name or None for a hard cut, frames). Even frame counts split evenly.
TRANSITIONS = {
    "cut": (None, 0), "fast_cut": (None, 0), "crossfade": ("fade", 12), "fade_black": ("fadeblack", 16),
    "whip_pan": ("smoothleft", 8), "zoom_through": ("zoomin", 10), "flash": ("fadewhite", 8),
    "slide": ("slideup", 10), "glitch": ("pixelize", 8),
}
WITHIN_SCENE = {"documentary": "crossfade"}          # other styles hard-cut between shots of one scene
SFX_VOLUME = {"whoosh": 0.45, "impact": 0.8, "riser": 0.5, "pop": 0.55, "ding": 0.45, "swoosh_soft": 0.35,
              "glitch": 0.45, "boom": 0.8, "click": 0.5}
SFX_LEAD = {"whoosh": 0.22, "swoosh_soft": 0.25, "riser": 1.2}   # these play slightly before their moment


def frames(seconds: float) -> int:
    return int(round(seconds * FPS))


def secs(f: int) -> float:
    return round(f / FPS, 4)


@dataclass
class Shot:
    scene_id: str
    index: int
    start: int                 # frames on the output timeline (boundary)
    end: int
    shot: dict                 # storyboard shot
    asset: dict | None         # assets.asset_dict() (None for graphics without background)
    trans_in: tuple[str | None, int] = (None, 0)
    trans_out: tuple[str | None, int] = (None, 0)

    @property
    def render_start(self) -> int:          # includes the overlap of the transition into this shot
        return self.start - self.trans_in[1] // 2

    @property
    def render_frames(self) -> int:
        return (self.end - self.start) + self.trans_in[1] // 2 + self.trans_out[1] // 2


@dataclass
class Scene:
    scene_id: str
    start: int
    end: int
    narration_at: float        # seconds
    audio: str | None
    words: list[dict]          # absolute times
    board: dict
    shots: list[Shot] = field(default_factory=list)


@dataclass
class Timeline:
    scenes: list[Scene]
    total_frames: int
    sfx: list[tuple[str, float, float]]      # (name, seconds, volume)

    @property
    def shots(self) -> list[Shot]:
        return [s for sc in self.scenes for s in sc.shots]

    @property
    def duration(self) -> float:
        return secs(self.total_frames)


def build(board: dict, voice: dict, assets_by_shot: dict, editing_style: str) -> Timeline:
    """board = storyboard, voice = narration summary, assets_by_shot = {(scene_id, idx): asset dict}."""
    vscenes = {v["scene_id"]: v for v in voice["scenes"]}
    t = 0.0
    scenes: list[Scene] = []
    n = len(board["scenes"])
    for k, sb in enumerate(board["scenes"]):
        v = vscenes.get(sb["scene_id"])
        if v is None:
            raise ValueError(f"Scene {sb['scene_id']} has no voiceover. Run Voiceover again.")
        pre = PAUSE_BEFORE_REVEAL if sb.get("purpose") in ("reveal", "payoff") else 0.0
        length = pre + v["duration"] + v.get("pause_after", 0.0) + (TAIL if k == n - 1 else 0.0)
        start_f, end_f = frames(t), frames(t + length)
        at = t + pre
        words = [{"word": w["word"], "start": round(at + w["start"], 3), "end": round(at + w["end"], 3)} for w in v.get("words", [])]
        sc = Scene(sb["scene_id"], start_f, end_f, round(at, 3), v.get("file"), words, sb)
        # split the scene between its shots by share (whole frames, every shot at least MIN_SHOT_FRAMES)
        shares = [max(0.01, float(s.get("share") or 0)) for s in sb["shots"]]
        total = sum(shares)
        span = end_f - start_f
        bounds, acc = [start_f], 0.0
        for sh in shares[:-1]:
            acc += sh / total
            bounds.append(start_f + int(round(span * acc)))
        bounds.append(end_f)
        for i in range(1, len(bounds) - 1):                      # enforce a minimum length where possible
            bounds[i] = max(bounds[i], bounds[i - 1] + MIN_SHOT_FRAMES)
            bounds[i] = min(bounds[i], end_f - MIN_SHOT_FRAMES * (len(bounds) - 1 - i))
        for i, shot in enumerate(sb["shots"]):
            sc.shots.append(Shot(sb["scene_id"], i, bounds[i], bounds[i + 1], shot, assets_by_shot.get((sb["scene_id"], i))))
        scenes.append(sc)
        t += length
    # transitions: inside a scene by style, between scenes as planned
    flat = [s for sc in scenes for s in sc.shots]
    within = WITHIN_SCENE.get(editing_style, "cut")
    for a, b in zip(flat, flat[1:]):
        name = within if a.scene_id == b.scene_id else next(sc.board for sc in scenes if sc.scene_id == a.scene_id).get("transition_out", "cut")
        xf, d = TRANSITIONS.get(name, (None, 0))
        d = min(d, (a.end - a.start) // 2 * 2, (b.end - b.start) // 2 * 2)      # never longer than either shot
        if d < 4:
            xf, d = None, 0
        a.trans_out = b.trans_in = (xf, d)
    return Timeline(scenes, scenes[-1].end if scenes else 0, sfx_cues(scenes))


def sfx_cues(scenes: list[Scene]) -> list[tuple[str, float, float]]:
    """Each scene's planned sound effects: the first on the scene's first cut, the rest on its following cuts."""
    cues = []
    for sc in scenes:
        names = [x for x in sc.board.get("sound_effects", []) if x and x != "none"]
        for name, shot in zip(names, sc.shots):
            at = max(0.0, secs(shot.start) - SFX_LEAD.get(name, 0.0))
            cues.append((name, round(at, 3), SFX_VOLUME.get(name, 0.5)))
    return cues


# --------------------------------------------------------------------------- subtitles + graphics (ASS)
def layout(fmt: str) -> dict:
    if fmt == "short":
        return {"w": 1080, "h": 1920, "caption_y": 1230, "lower_y": 1480, "graphic_y": 760, "kicker_y": 300, "scale": 1.0}
    return {"w": 1920, "h": 1080, "caption_y": 860, "lower_y": 940, "graphic_y": 430, "kicker_y": 110, "scale": 0.62}


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Segoe UI Black,{cap},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,{o8},{o4},5,70,70,0,1
Style: Lower,Segoe UI Semibold,{low},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,3,{o12},0,2,110,110,{lowm},1
Style: Minimal,Segoe UI Semibold,{low},&H00FFFFFF,&H00FFFFFF,&H00000000,&H50000000,-1,0,0,0,100,100,0,0,1,{o3},{o2},2,110,110,{lowm},1
Style: Big,Segoe UI Black,{big},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,2,0,1,{o10},{o6},5,90,90,0,1
Style: Sub,Segoe UI Black,{sub},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,{o6},{o3},5,90,90,0,1
Style: Card,Segoe UI Black,{card},&H00FFFFFF,&H00FFFFFF,&H00141414,&HC8141414,-1,0,0,0,100,100,1,0,3,{o22},0,5,110,110,0,1
Style: Kicker,Segoe UI Black,{kick},&H00141414,&H00141414,&H001FD2FF,&H00000000,-1,0,0,0,100,100,1,0,3,{o14},0,5,120,120,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

ACCENT = r"\c&H1FD2FF&"       # warm yellow (BGR)
WHITE = r"\c&HFFFFFF&"


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def _big_size(text: str, base: int) -> int:
    n = len(text)
    return base if n <= 8 else int(base * 0.8) if n <= 14 else int(base * 0.62) if n <= 22 else int(base * 0.5)


def graphic_events(shot: Shot, lay: dict) -> list[str]:
    g = shot.shot.get("graphic") or {}
    kind, text, sub = g.get("kind", "none"), _esc(g.get("text", "")), _esc(g.get("subtext", ""))
    if kind == "none" or not text:
        return []
    s = lay["scale"]
    st, en = cap.ts(secs(shot.start) + 0.04), cap.ts(secs(shot.end))
    x, y = lay["w"] // 2, lay["graphic_y"]
    out = []
    pop = r"\fscx40\fscy40\t(0,140,\fscx112\fscy112)\t(140,240,\fscx100\fscy100)\fad(60,120)"
    if kind in ("text_punch", "number_reveal", "stat_callout", "ranking_card"):
        # one event, main text + subtitle on its own line: libass lays out the lines, so they can never overlap
        size = int(_big_size(text, 230 if kind != "text_punch" else 200) * s)
        color = ACCENT if kind != "text_punch" else WHITE
        rot = r"\frz-3" if kind == "text_punch" else ""
        subline = rf"\N{{\fs{int(max(60, _big_size(sub, 88)) * s)}\c&HFFFFFF&\bord{max(1, int(6 * s))}}}{sub.upper()}" if sub else ""
        out.append(rf"Dialogue: 5,{st},{en},Big,,0,0,0,,{{\an5\pos({x},{y})\fs{size}{color}{rot}{pop}}}{text}{subline}")
    elif kind == "quote_card":
        out.append(rf"Dialogue: 5,{st},{en},Card,,0,0,0,,{{\an5\pos({x},{y})\i1\fs{int(max(66, _big_size(text, 92)) * s)}\fad(150,150)}}“{text}”"
                   + (rf"\N{{\i0\fs{int(50 * s)}{ACCENT}}}— {sub}" if sub else ""))
    else:  # title_card, comparison, timeline, map_pin
        prefix = "▼ " if kind == "map_pin" else ""
        out.append(rf"Dialogue: 5,{st},{en},Card,,0,0,0,,{{\an5\pos({x},{y})\fs{int(max(72, _big_size(text, 130)) * s)}{pop}}}"
                   rf"{prefix}{text}"
                   + (rf"\N{{\fs{int(54 * s)}{ACCENT}}}{sub}" if sub else ""))
    return out


def kicker_events(sc: Scene, lay: dict, avatar: bool) -> list[str]:
    """The scene's on-screen text, top of frame, until the scene's first graphic (graphics carry their own text)."""
    text = _esc(sc.board.get("on_screen_text", ""))
    if not text:
        return []
    start = secs(sc.start) + 0.1
    first_graphic = next((secs(s.start) for s in sc.shots if (s.shot.get("graphic") or {}).get("kind", "none") != "none"), None)
    end = min(secs(sc.end), start + 2.8, first_graphic if first_graphic is not None else 1e9)
    if end - start < 0.6:
        return []
    y = lay["kicker_y"] + (300 if avatar and lay["w"] < lay["h"] else 0)
    size = int((72 if len(text) <= 24 else 60 if len(text) <= 36 else 52) * lay["scale"])
    return [f"Dialogue: 4,{cap.ts(start)},{cap.ts(end)},Kicker,,0,0,0,,"
            f"{{\\pos({lay['w'] // 2},{y})\\fs{size}\\fad(120,150)\\fscx80\\fscy80\\t(0,120,\\fscx100\\fscy100)}}{text.upper()}"]


def lower_events(words: list[dict], style: str, max_words: int = 7) -> list[str]:
    """Calm sentence-chunk captions for documentary / explainer styles."""
    out, chunk = [], []
    for w in words:
        chunk.append(w)
        end_sentence = w["word"][-1:] in ".!?" if w["word"] else False
        if len(chunk) >= max_words or end_sentence:
            out.append(chunk)
            chunk = []
    if chunk:
        out.append(chunk)
    ev = []
    for i, c in enumerate(out):
        end = min(c[-1]["end"] + 0.3, out[i + 1][0]["start"]) if i + 1 < len(out) else c[-1]["end"] + 0.4
        ev.append(f"Dialogue: 1,{cap.ts(c[0]['start'])},{cap.ts(end)},{style},,0,0,0,,{{\\fad(80,80)}}"
                  + _esc(" ".join(x["word"] for x in c)))
    return ev


def build_ass(tl: Timeline, caption_style: str, fmt: str, avatar: bool = False) -> str:
    lay = layout(fmt)
    s = lay["scale"]
    o = {f"o{k}": max(1, int(k * s)) for k in (2, 3, 4, 6, 8, 10, 12, 14, 22)}
    header = ASS_HEADER.format(w=lay["w"], h=lay["h"], cap=int(92 * s), low=int(58 * s), lowm=lay["h"] - lay["lower_y"],
                               big=int(200 * s), sub=int(70 * s), card=int(90 * s), kick=int(56 * s), **o)
    lines = [header]
    words = [w for sc in tl.scenes for w in sc.words]
    # captions: script words (correct spelling) with Whisper timing
    for sc in tl.scenes:
        lines += kicker_events(sc, lay, avatar)
        for sh in sc.shots:
            lines += graphic_events(sh, lay)
    if caption_style in ("documentary_lower",):
        lines += lower_events(words, "Lower")
    elif caption_style == "clean_minimal":
        lines += lower_events(words, "Minimal", 6)
    else:   # dynamic, bold_center, countdown: word-by-word pop captions
        y = lay["caption_y"] + (-80 if caption_style == "bold_center" else 0)
        lines += cap.caption_events(words, "Caption", (lay["w"] // 2, y), cap.YELLOW, True)
    return "\n".join(lines) + "\n"


def caption_style_of(board: dict, editing_style: str) -> str:
    styles_used = [sc.get("caption_style") for sc in board["scenes"] if sc.get("caption_style")]
    return max(set(styles_used), key=styles_used.count) if styles_used else styles.style(editing_style)["caption_style"]
