"""Hallucination filtering and audio quality checks."""
from __future__ import annotations

import re

# Known Whisper hallucination artifacts — both EN and NL
HALLUCINATION_PATTERNS: set[str] = {
    "thank you for watching",
    "thanks for watching",
    "subscribe",
    "subtitles by",
    "translated by",
    "copyright",
    "all rights reserved",
    "ondertiteling door",
    "vertaald door",
    "alle rechten voorbehouden",
    "amara.org",
    "tv gelderland",
    # Meeting-specific artifacts
    "you're on mute",
    "can you hear me",
    "unmute yourself",
}

# Pattern for repeated phrases (Whisper sometimes loops)
_REPEAT_PATTERN = re.compile(r"(.{10,}?)\1{2,}", re.IGNORECASE)


def strip_hallucinations(text: str) -> str:
    """Remove known Whisper hallucination artifacts from text."""
    text_lower = text.lower()
    for pattern in HALLUCINATION_PATTERNS:
        while pattern in text_lower:
            idx = text_lower.find(pattern)
            text = text[:idx] + text[idx + len(pattern) :]
            text_lower = text.lower()

    # Remove repeated phrases (3+ repetitions of 10+ chars)
    text = _REPEAT_PATTERN.sub(r"\1", text)

    return " ".join(text.split()).strip()


def is_low_confidence(no_speech_prob: float, avg_logprob: float) -> bool:
    """Check if a segment is likely noise / not speech.

    Two nets: a very high no-speech probability alone, or a moderately high one combined
    with low decode confidence. (The cli backend has no no_speech_prob and passes 0.0, so it
    relies on VAD upstream; the server backend feeds real values here.)
    """
    return no_speech_prob > 0.85 or (no_speech_prob > 0.6 and avg_logprob < -1.0)
