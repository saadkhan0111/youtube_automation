"""Studio configuration: option lists and model names (all models configurable via environment variables)."""
from __future__ import annotations

import os
from pathlib import Path

from ..clipper import settings as clip_settings
from ..config import settings

# Models (never hard-coded elsewhere: import from here)
# Research needs a strong model with web search: in testing gpt-5.5 ran 10 searches and cited Rockstar's own
# pages, while gpt-4o-mini ran 1 search and cited fan sites. Structuring the notes is easy -> cheaper model.
RESEARCH_MODEL = os.environ.get("STUDIO_RESEARCH_MODEL", "gpt-5.5")
STRUCTURE_MODEL = os.environ.get("STUDIO_STRUCTURE_MODEL", clip_settings.OPENAI_MODEL)
# Script quality decides whether the video feels human: in testing gpt-5.5 wrote specific, natural narration on
# target length, while gpt-4o-mini wrote generic hype ("Yup! That's right!") 26% too short.
SCRIPT_MODEL = os.environ.get("STUDIO_SCRIPT_MODEL", "gpt-5.5")
# In testing gpt-5.5 planned 30 varied shots that matched each line; gpt-4o-mini planned vague shots and empty graphics.
STORYBOARD_MODEL = os.environ.get("STUDIO_STORYBOARD_MODEL", "gpt-5.5")
IMAGE_MODEL = os.environ.get("STUDIO_IMAGE_MODEL", "gpt-image-1.5")
TTS_MODEL = clip_settings.OPENAI_TTS_MODEL
TRANSCRIBE_MODEL = clip_settings.OPENAI_TRANSCRIBE_MODEL

PEXELS_API_KEY_ENV = "PEXELS_API_KEY"
JAMENDO_CLIENT_ID_ENV = "JAMENDO_CLIENT_ID"

CATEGORIES = {
    "gaming": "Gaming facts & explainers",
    "tech": "Technology & AI",
    "history": "History & mystery",
    "science": "Science & education",
    "countdown": "Top 5 / Top 10",
    "general": "General information",
}
FORMATS = {"short": "Short (9:16)", "long": "Long (16:9)"}
DURATION_LIMITS = {"short": (15, 180), "long": (120, 1800)}       # seconds
EDITING_STYLES = {
    "viral": "Viral & energetic",
    "documentary": "Cinematic documentary",
    "explainer": "Educational explainer",
    "countdown": "Top 5 / Top 10 countdown",
}
NARRATION_STYLES = {
    "energetic": "Energetic & hyped",
    "dramatic": "Dramatic & suspenseful",
    "conversational": "Conversational & friendly",
    "authoritative": "Calm & authoritative",
    "storyteller": "Storyteller",
}
LANGUAGES = {
    "en": "English", "ur": "Urdu", "hi": "Hindi", "ar": "Arabic", "es": "Spanish", "fr": "French",
    "de": "German", "pt": "Portuguese", "tr": "Turkish", "id": "Indonesian",
}
VOICES = clip_settings.TTS_VOICES
DEFAULT_VOICE = clip_settings.OPENAI_TTS_VOICE

# Voiceover runs before Assets: footage is picked and trimmed to the REAL length of each recorded scene.
STAGES = ["research", "script", "storyboard", "voice", "assets", "render", "qa", "complete"]
STAGE_LABELS = {
    "research": "Research", "script": "Script", "storyboard": "Storyboard", "assets": "Assets",
    "voice": "Voiceover", "render": "Rendering", "qa": "Quality check", "complete": "Complete",
}


def studio_dir() -> Path:
    d = settings.data_dir / "studio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir(project_id: int) -> Path:
    d = studio_dir() / str(project_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def options() -> dict:
    """Everything the New project form needs."""
    return {
        "categories": CATEGORIES, "formats": FORMATS, "duration_limits": DURATION_LIMITS,
        "editing_styles": EDITING_STYLES, "narration_styles": NARRATION_STYLES, "languages": LANGUAGES,
        "voices": VOICES, "default_voice": DEFAULT_VOICE,
        "stages": [{"key": s, "label": STAGE_LABELS[s]} for s in STAGES],
        "openai_key_present": clip_settings.openai_available(),
        "pexels_key_present": bool(os.environ.get(PEXELS_API_KEY_ENV)),
        "jamendo_key_present": bool(os.environ.get(JAMENDO_CLIENT_ID_ENV)),
        "storyboard": _storyboard_options(),
        "models": {"research": RESEARCH_MODEL, "structure": STRUCTURE_MODEL, "script": SCRIPT_MODEL, "storyboard": STORYBOARD_MODEL,
                   "image": IMAGE_MODEL, "tts": TTS_MODEL, "transcribe": TRANSCRIBE_MODEL},
    }


def _storyboard_options() -> dict:
    from . import styles as st

    return {"visual_types": st.VISUAL_TYPES, "graphic_kinds": st.GRAPHIC_KINDS, "camera": st.CAMERA_MOVES,
            "effects": st.EFFECTS, "transitions": st.TRANSITIONS, "sfx": st.SOUND_EFFECTS, "moods": st.MUSIC_MOODS}
