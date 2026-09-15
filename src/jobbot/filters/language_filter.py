"""Pass 0.5: reject descriptions that aren't Hebrew or English before spending regex/Gemini calls."""

from langdetect import DetectorFactory, LangDetectException, detect

# Deterministic results - langdetect is otherwise randomized per-call for short/ambiguous text.
DetectorFactory.seed = 0

ALLOWED_LANGUAGES = {"he", "en"}

# Below this length, detection is unreliable - don't reject on a guess.
MIN_TEXT_LENGTH = 30


def is_allowed_language(text: str) -> bool:
    """True if the text is Hebrew, English, too short to reliably tell, or detection fails."""
    if not text or len(text.strip()) < MIN_TEXT_LENGTH:
        return True

    try:
        return detect(text) in ALLOWED_LANGUAGES
    except LangDetectException:
        return True
