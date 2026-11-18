"""ASS subtitle generation: headline title + word-by-word animated captions.

Caption design (Shorts/TikTok style):
  * big bold Segoe UI Black, white with a thick black outline and soft shadow
  * 1-3 words per group, split at pauses and punctuation; short function words are kept with
    the word they belong to ("THE BOAT", not "THE" | "BOAT")
  * each group pops in; the word being spoken scales up and takes the highlight colour
  * the most important word of a group (names, numbers, action words) stays highlighted:
    yellow for game audio, cyan for the avatar's voice
Timing comes unchanged from the Whisper word timestamps (or spread_words for TTS text).
"""
from __future__ import annotations

import re

PROFANITY = re.compile(r"\b(motherfuck\w*|fuck\w*|shit\w*|bitch\w*|cunt\w*|asshole\w*|dick\w*|bastard\w*|nigg\w*|fag\w*)",
                       re.IGNORECASE)

CAPTION_FONT = "Segoe UI Black"   # falls back to the system default if missing (fonts are copied from C:\Windows\Fonts)
CAPTION_SIZE = 92
VOICE_SIZE = 86

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,Arial Black,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,6,3,8,{title_ml},50,{title_y},1
Style: Caption,{font},{csize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,8,4,5,70,70,0,1
Style: Voice,{font},{vsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,8,4,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

YELLOW = r"\c&H00E6FF&"   # ASS colours are BGR
CYAN = r"\c&HFFE13C&"
WHITE = r"\c&HFFFFFF&"

MAX_WORDS = 3
MAX_CHARS = 15        # keeps one line at 92 px
PAUSE = 0.35          # a silence longer than this starts a new group

STOPWORDS = set("""a an the and or but so if of to in on at by for from with as is are was were be been am i me my
you your he him his she her it its we us our they them their this that these those there here what who how
do does did done have has had will would can could should just not no yes oh uh um ah okay ok yeah hey well
get got go going gonna gotta wanna let lets let's im i'm you're it's that's don't can't won't up out all like
before after because about really very some something where when then than right now still even only""".split())
FUNCTION_WORDS = {"a", "an", "the", "to", "of", "in", "on", "at", "for", "my", "your", "his", "her", "our", "their",
                  "and", "or", "but", "with", "from", "by", "this", "that", "is", "are", "was", "i", "you", "we",
                  # words that lead into the next word: don't leave them dangling at the end of a group
                  "get", "go", "gonna", "gotta", "wanna", "want", "let", "that's", "it's", "i'm", "you're", "we're",
                  "they're", "i'll", "i've", "can't", "don't", "won't", "can", "could", "will", "would", "should",
                  "must", "just", "so", "then", "be", "have", "has", "had", "not", "very", "really"}
HYPE = set("""boat yacht car crash crashed explosion explode boom shot shoot shooting gun police cops cop wanted chase
run jump fall falling fire dead die killed kill money heist steal stole stolen insane crazy wow omg help hurry fast
faster engine water plane bike tank escape jail bust busted win won lose lost epic huge massive never always""".split())


def ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def clean(text: str, mask_profanity: bool = True) -> str:
    text = text.replace("\\", "").replace("{", "(").replace("}", ")").strip()
    if mask_profanity:
        text = PROFANITY.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)
    return text


def _base(word: str) -> str:
    return re.sub(r"[^\w']", "", word).lower()


def _chars(group: list[dict]) -> int:
    return sum(len(w["word"]) for w in group) + max(0, len(group) - 1)


def _phrases(words: list[dict], max_gap: float) -> list[list[dict]]:
    """Split at pauses and sentence/clause punctuation: groups never span these."""
    phrases: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > max_gap or cur[-1]["word"].rstrip().endswith((".", "!", "?", ",", ";", ":"))):
            phrases.append(cur)
            cur = []
        cur.append(w)
    if cur:
        phrases.append(cur)
    return phrases


def _group_cost(g: list[dict], max_chars: int) -> float:
    if _chars(g) > max_chars and len(g) > 1:
        return 1e9
    cost = 1.0                                       # fewer, fuller groups read better
    if len(g) > 1 and _base(g[-1]["word"]) in FUNCTION_WORDS:
        cost += 2.0                                  # "GET ME TO" | "THE BOAT" reads badly
    if len(g) == 1 and len(_base(g[0]["word"])) <= 4 and keyword_score(g[0]["word"]) < 3.5:
        cost += 1.5                                  # lone short word flashes by
    return cost


def group_words(words: list[dict], max_words: int = MAX_WORDS, max_chars: int = MAX_CHARS,
                max_gap: float = PAUSE) -> list[list[dict]]:
    """Readable caption groups: split into phrases, then choose the best 1-3 word split of each
    phrase (dynamic programming over a small readability cost)."""
    out: list[list[dict]] = []
    for ph in _phrases(words, max_gap):
        n = len(ph)
        best = [0.0] + [float("inf")] * n
        back = [0] * (n + 1)
        for i in range(1, n + 1):
            for k in range(1, min(max_words, i) + 1):
                c = best[i - k] + _group_cost(ph[i - k:i], max_chars)
                if c < best[i]:
                    best[i], back[i] = c, i - k
        cuts, i = [], n
        while i > 0:
            cuts.append((back[i], i))
            i = back[i]
        out.extend(ph[a:b] for a, b in reversed(cuts))
    return out


def keyword_score(word: str, first_in_sentence: bool = False) -> float:
    b = _base(word)
    if not b or b in STOPWORDS:
        return 0.0
    score = min(len(b), 8) * 0.5
    if b in HYPE or "*" in word:
        score += 3
    if any(ch.isdigit() for ch in b):
        score += 3
    if word.rstrip().endswith(("!", "?")):
        score += 1.5
    if word[:1].isupper() and not first_in_sentence:
        score += 1.5          # proper noun (Whisper keeps capitals)
    return score


def pick_keyword(group: list[dict], prev_word: str | None) -> int | None:
    """Index of the word to emphasise in a group (at most one), or None."""
    best, best_i = 0.0, None
    for i, w in enumerate(group):
        before = group[i - 1]["word"] if i else prev_word
        first = before is None or before.rstrip().endswith((".", "!", "?"))
        s = keyword_score(w["word"], first)
        if s > best:
            best, best_i = s, i
    return best_i if best >= 3.5 else None


def caption_events(words: list[dict], style: str, pos: tuple[int, int], highlight: str, mask: bool) -> list[str]:
    events = []
    groups = group_words([w for w in words if w["word"].strip()])
    prev_word = None
    for gi, g in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else None
        group_end = g[-1]["end"] + 0.35
        if next_start is not None:
            group_end = min(group_end, next_start)
        texts = [clean(w["word"], mask).upper() for w in g]
        key = pick_keyword(g, prev_word)
        prev_word = g[-1]["word"]
        for j, w in enumerate(g):
            st = w["start"]
            en = g[j + 1]["start"] if j + 1 < len(g) else group_end
            if en <= st:
                en = st + 0.08
            parts = []
            for k, t in enumerate(texts):
                if k == j:   # word being spoken: highlight colour + quick scale pop
                    parts.append("{" + highlight + r"\fscx100\fscy100\t(0,70,\fscx122\fscy122)\t(70,160,\fscx108\fscy108)}"
                                 + t + "{" + WHITE + r"\fscx100\fscy100}")
                elif k == key:  # important word stays highlighted
                    parts.append("{" + highlight + "}" + t + "{" + WHITE + "}")
                else:
                    parts.append(t)
            # the whole group bounces in on its first word (slides up + scale overshoot + fade); later words just update
            if j == 0:
                place = (rf"\move({pos[0]},{pos[1] + 26},{pos[0]},{pos[1]},0,130)"
                         r"\fscx72\fscy72\alpha&H60&\t(0,90,\fscx106\fscy106\alpha&H00&)\t(90,150,\fscx100\fscy100)")
            else:
                place = rf"\pos({pos[0]},{pos[1]})"
            events.append(f"Dialogue: 1,{ts(st)},{ts(en)},{style},,0,0,0,,{{{place}}}" + " ".join(parts))
    return events


def spread_words(text: str, start: float, duration: float) -> list[dict]:
    """Approximate word timings for TTS text (proportional to word length)."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [len(t) + 2 for t in tokens]
    total = sum(weights)
    out, t = [], start
    for tok, wt in zip(tokens, weights):
        d = duration * wt / total
        out.append({"start": t, "end": t + d * 0.92, "word": tok})
        t += d
    return out


def build_ass(headline: str, duration: float, game_words: list[dict], voice_words: list[dict], caption_y: int,
              title_y: int = 230, mask_profanity: bool = True, title_margin_l: int = 70,
              card_end: float | None = None, card_y: int | None = None) -> str:
    """Full subtitle file. The headline is shown only as the opening hook card (until `card_end`);
    captions start when the card leaves, so the two never compete on screen."""
    header = (HEADER.replace("{title_y}", str(title_y)).replace("{title_ml}", str(title_margin_l))
              .replace("{font}", CAPTION_FONT).replace("{csize}", str(CAPTION_SIZE)).replace("{vsize}", str(VOICE_SIZE)))
    lines = [header]
    card_until = 0.0
    if headline:
        card_until = min(duration - 0.5, card_end if card_end is not None else card_duration(None))
        lines += hook_card_events(headline, card_until, card_y if card_y is not None else caption_y, mask_profanity)

    def after_card(ws: list[dict]) -> list[dict]:
        return [w for w in ws if w["start"] >= card_until - 0.05]

    lines += caption_events(after_card(game_words), "Caption", (540, caption_y), YELLOW, mask_profanity)
    lines += caption_events(after_card(voice_words), "Voice", (540, caption_y), CYAN, mask_profanity)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- opening hook card
CARD_FONT_FILES = ["C:/Windows/Fonts/seguibl.ttf", "C:/Windows/Fonts/ariblk.ttf"]
CARD_MAX_W = 900           # max text width in px (card adds padding)
CARD_PAD_X, CARD_PAD_Y = 46, 26
CARD_RADIUS = 26
CARD_TILT = -2.5           # degrees
CARD_YELLOW = "&H1FD2FF&"  # #FFD21F in BGR
CARD_MIN, CARD_MAX = 2.2, 3.2


def _measure(text: str, size: int) -> int:
    """Rendered width of `text` in px at `size` (uses the real font file when available)."""
    try:
        from PIL import ImageFont

        for f in CARD_FONT_FILES:
            try:
                font = ImageFont.truetype(f, size)
                return int(font.getlength(text))
            except OSError:
                continue
    except ImportError:
        pass
    return int(len(text) * size * 0.66)


def layout_card_text(text: str, size: int = 100, max_w: int = CARD_MAX_W) -> tuple[list[str], int, int]:
    """Split into 1-3 balanced lines and pick the largest font size that fits. Returns (lines, size, width)."""
    words = text.split()
    n = len(words)
    for sz in range(size, 55, -6):
        for nlines in (1, 2, 3):
            if nlines > n:
                break
            if nlines == 1:
                splits = [[text]]
            elif nlines == 2:
                splits = [[" ".join(words[:i]), " ".join(words[i:])] for i in range(1, n)]
            else:
                splits = [[" ".join(words[:i]), " ".join(words[i:j]), " ".join(words[j:])]
                          for i in range(1, n - 1) for j in range(i + 1, n)]
            best = None
            for lines in splits:
                w = max(_measure(line, sz) for line in lines)
                if w <= max_w and (best is None or w < best[2]):
                    best = (lines, sz, w)
            if best:
                return best
    return [text], 56, min(max_w, _measure(text, 56))


def _rounded_rect(w: int, h: int, r: int) -> str:
    """ASS vector drawing of a rounded rectangle with its top-left at (0,0)."""
    k = r * 0.45  # bezier handle
    return (f"m {r} 0 l {w - r} 0 b {w - k} 0 {w} {k} {w} {r} l {w} {h - r} b {w} {h - k} {w - k} {h} {w - r} {h} "
            f"l {r} {h} b {k} {h} 0 {h - k} 0 {h - r} l 0 {r} b 0 {k} {k} 0 {r} 0")


def card_duration(hook_end: float | None) -> float:
    return max(CARD_MIN, min(CARD_MAX, (hook_end or 0) + 0.15))


def hook_card_events(headline: str, end: float, center_y: int, mask_profanity: bool = True) -> list[str]:
    """Opening title card: pops in with a slight overshoot, holds, then shrinks/fades out at `end`."""
    text = clean(headline, mask_profanity).upper()
    if not text:
        return []
    lines, size, text_w = layout_card_text(text)
    line_h = int(size * 1.12)
    box_w = text_w + 2 * CARD_PAD_X
    box_h = line_h * len(lines) + 2 * CARD_PAD_Y
    cx, cy = 540, center_y
    ms = int(end * 1000)
    exit_start = max(400, ms - 260)
    # Identical motion for box and text: they rotate/scale around the same origin.
    # Transparency is animated per layer so the box's soft shadow keeps its own opacity.
    def motion(hidden: str, shown: str) -> str:
        return (rf"\org({cx},{cy})\frz{CARD_TILT}\fscx20\fscy20{hidden}"
                rf"\t(0,140,\fscx112\fscy112{shown})\t(140,260,\fscx97\fscy97)\t(260,340,\fscx100\fscy100)"
                rf"\t({exit_start},{ms},\fscx70\fscy70{hidden}\frz{CARD_TILT - 4})")

    box_motion = motion(r"\1a&HFF&\4a&HFF&", r"\1a&H00&\4a&H70&")
    txt_motion = motion(r"\alpha&HFF&", r"\alpha&H00&")
    box = (rf"{{\an5\pos({cx},{cy}){box_motion}\bord0\shad10\4c&H000000&\1c{CARD_YELLOW}\p1}}"
           + _rounded_rect(box_w, box_h, CARD_RADIUS) + r"{\p0}")
    txt = (rf"{{\an5\pos({cx},{cy}){txt_motion}\fn{CAPTION_FONT}\fs{size}\bord0\shad0\1c&H101010&\q2}}"
           + r"\N".join(lines))
    return [f"Dialogue: 4,{ts(0)},{ts(end)},Caption,,0,0,0,,{box}",
            f"Dialogue: 5,{ts(0)},{ts(end)},Caption,,0,0,0,,{txt}"]
