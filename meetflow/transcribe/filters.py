"""Hallucination filtering and audio quality checks."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

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
    "ondertiteld door",
    "vertaald door",
    "alle rechten voorbehouden",
    "amara.org",
    "tv gelderland",
    "bedankt voor het kijken",
    "tot de volgende keer",
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

    result = " ".join(text.split()).strip()
    # Drop a segment that is now pure punctuation / symbols ("***", "...", "- -"): a trailing-silence
    # artifact Whisper emits as its own segment. Without this it reaches the transcript-of-record
    # (seen as the "***" tail in 2026-06-19). A bare number keeps a word-char and survives.
    if result and not re.search(r"\w", result):
        return ""
    return result


# ── consecutive-loop collapse ───────────────────────────────────────────────────
_COLLISION_S = 1.0  # identical segments whose starts fall within this are one VAD-window loop


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip(" .,!?;:—-")


def collapse_repeated_segments(segments: list) -> list:
    """Collapse Whisper decoder loops: runs of consecutive segments with identical text.

    Whisper can loop a sentence many times inside/across VAD windows (seen in a real journal:
    one sentence emitted 17x across two runs). Two triggers, matching the loop shapes in the data:
      - timestamp-collision: a run of >= 2 identical segments whose starts fall within _COLLISION_S
        (the same VAD window emitted repeatedly).
      - pure-consecutive: a run of >= 3 identical segments regardless of timing. A length-2 verbatim
        repeat is left alone (a real "ja. ja." is plausible; 3x back-to-back is a loop).
    Keeps the FIRST segment of each collapsed run and extends its end to the last, so the timeline
    stays covered; drops the rest. Logs each collapse (auditable when reading meeting.md). Operates
    on any object with .start/.end/.text; mutates .end in place and returns the kept list.
    """
    if not segments:
        return segments
    kept: list = []
    i, n = 0, len(segments)
    while i < n:
        base = _norm(getattr(segments[i], "text", ""))
        j = i + 1
        while j < n and base and _norm(getattr(segments[j], "text", "")) == base:
            j += 1
        run = j - i
        if run >= 2:
            starts = [getattr(segments[k], "start", 0.0) for k in range(i, j)]
            colliding = (max(starts) - min(starts)) <= _COLLISION_S
            if run >= 3 or colliding:
                first, last = segments[i], segments[j - 1]
                first.end = max(getattr(first, "end", 0.0), getattr(last, "end", 0.0))
                kept.append(first)
                log.info(
                    "collapsed %d looped segments @%.1fs: %r",
                    run, getattr(first, "start", 0.0), (getattr(first, "text", "") or "")[:60],
                )
                i = j
                continue
        kept.append(segments[i])
        i += 1
    return kept


def is_low_confidence(no_speech_prob: float, avg_logprob: float) -> bool:
    """Check if a segment is likely noise / not speech.

    Two nets: a very high no-speech probability alone, or a moderately high one combined
    with low decode confidence. (The cli backend has no no_speech_prob and passes 0.0, so it
    relies on VAD upstream; the server backend feeds real values here.)
    """
    return no_speech_prob > 0.85 or (no_speech_prob > 0.6 and avg_logprob < -1.0)
