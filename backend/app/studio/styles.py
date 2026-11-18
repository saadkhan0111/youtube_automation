"""Editing-style configurations. The storyboard, renderer and subtitle engine all read from here, so each
style has one consistent definition of pacing, transitions, effects, captions and music."""
from __future__ import annotations

VISUAL_TYPES = ["stock_video", "stock_image", "ai_image", "gameplay", "user_footage", "graphic"]
GRAPHIC_KINDS = ["title_card", "number_reveal", "stat_callout", "ranking_card", "comparison", "quote_card",
                 "timeline", "map_pin", "text_punch"]
CAMERA_MOVES = ["static", "slow_push_in", "slow_pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down",
                "zoom_punch", "parallax", "handheld_drift"]
EFFECTS = ["film_grain", "vignette", "color_pop", "light_leak", "glow", "shake", "speed_ramp", "blur_in",
           "chromatic_aberration", "letterbox"]
TRANSITIONS = ["cut", "fast_cut", "crossfade", "fade_black", "whip_pan", "zoom_through", "flash", "slide", "glitch"]
SOUND_EFFECTS = ["whoosh", "impact", "riser", "pop", "ding", "swoosh_soft", "glitch", "boom", "click", "none"]
MUSIC_MOODS = ["energetic", "upbeat", "epic", "suspense", "mysterious", "dark", "emotional", "chill"]
CAPTION_STYLES = ["dynamic", "bold_center", "documentary_lower", "clean_minimal", "countdown"]

STYLES: dict[str, dict] = {
    "viral": {
        "shot_seconds": (1.5, 3.0),
        "transitions": ["fast_cut", "whip_pan", "zoom_through", "flash", "cut"],
        "camera": ["zoom_punch", "slow_push_in", "handheld_drift", "pan_left", "pan_right"],
        "effects": ["color_pop", "shake", "glow", "speed_ramp", "chromatic_aberration"],
        "sfx": ["whoosh", "impact", "pop", "boom", "glitch"],
        "music": ["energetic", "upbeat", "epic"],
        "caption_style": "dynamic",
        "brief": "Fast cuts, bold text punches on key words, punchy sound effects, high energy. Change the visual every 1.5-3 s.",
    },
    "documentary": {
        "shot_seconds": (3.0, 6.0),
        "transitions": ["crossfade", "fade_black", "cut"],
        "camera": ["slow_push_in", "slow_pull_out", "pan_left", "pan_right", "tilt_up", "parallax"],
        "effects": ["film_grain", "vignette", "letterbox", "light_leak"],
        "sfx": ["riser", "swoosh_soft", "boom", "none"],
        "music": ["suspense", "mysterious", "emotional", "dark", "epic"],
        "caption_style": "documentary_lower",
        "brief": "Atmospheric, cinematic slow camera moves, subtle transitions, emotional music. Let shots breathe (3-6 s).",
    },
    "explainer": {
        "shot_seconds": (3.0, 5.0),
        "transitions": ["cut", "slide", "crossfade"],
        "camera": ["static", "slow_push_in", "pan_left", "pan_right"],
        "effects": ["color_pop", "vignette"],
        "sfx": ["pop", "ding", "click", "swoosh_soft", "none"],
        "music": ["chill", "upbeat"],
        "caption_style": "clean_minimal",
        "brief": "Clear and visual: diagrams, stat callouts, comparisons and timelines for every number or concept.",
    },
    "countdown": {
        "shot_seconds": (2.0, 4.0),
        "transitions": ["whip_pan", "zoom_through", "flash", "fast_cut"],
        "camera": ["slow_push_in", "zoom_punch", "pan_left", "pan_right"],
        "effects": ["color_pop", "glow", "shake"],
        "sfx": ["whoosh", "riser", "impact", "ding"],
        "music": ["energetic", "suspense", "epic"],
        "caption_style": "countdown",
        "brief": "Every item opens with an animated ranking card (number + name), quick reveals, suspense before #1.",
    },
}


def style(name: str) -> dict:
    return STYLES.get(name, STYLES["viral"])
