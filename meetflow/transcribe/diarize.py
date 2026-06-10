"""Channel-based diarization — split 2-channel WAV, transcribe each, merge by timestamp."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from meetflow.config import WhisperConfig
from meetflow.transcribe.engine import transcribe_audio

log = logging.getLogger(__name__)


@dataclass
class DiarizedSegment:
    """A transcribed segment with speaker attribution."""

    speaker: str  # "me" or "them"
    start: float
    end: float
    text: str
    language: str


def load_stereo_wav(wav_path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """Load a 2-channel WAV and return (mic_audio, loopback_audio, sample_rate)."""
    data, sr = sf.read(str(wav_path), dtype="float32")

    if data.ndim == 1:
        log.warning("Mono WAV detected — treating as mic-only audio")
        return data, np.zeros_like(data), sr

    if data.shape[1] < 2:
        log.warning("Single-channel WAV — treating as mic-only audio")
        return data[:, 0], np.zeros(len(data), dtype=np.float32), sr

    return data[:, 0], data[:, 1], sr


def _has_speech(audio: np.ndarray, sample_rate: int = 16_000) -> bool:
    """Check if audio has enough energy and duration to be speech."""
    if len(audio) < sample_rate:  # Less than 1 second — skip
        return False
    rms = np.sqrt(np.mean(audio**2))
    return rms > 1e-4  # Stricter threshold — filters out low-level noise


def _text_similarity(a: str, b: str) -> float:
    """Word-level Jaccard similarity — fast, sufficient for near-duplicate detection."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _temporal_overlap(seg_a: DiarizedSegment, seg_b: DiarizedSegment) -> float:
    """Overlap ratio between two segments (0-1)."""
    overlap = max(0.0, min(seg_a.end, seg_b.end) - max(seg_a.start, seg_b.start))
    min_dur = min(seg_a.end - seg_a.start, seg_b.end - seg_b.start)
    return overlap / min_dur if min_dur > 0 else 0.0


def _deduplicate_cross_channel(
    segments: list[DiarizedSegment],
    mic_audio: np.ndarray,
    loopback_audio: np.ndarray,
    sr: int,
    aec_enabled: bool = False,
) -> list[DiarizedSegment]:
    """Remove far-side bleed the mic picked up and Whisper transcribed a second time.

    Tap-anchored asymmetric attribution: the system-audio tap (channel 1, "them") is
    ground truth for the far side. When a "me" and a "them" segment are near-duplicates
    (overlapping in time, similar text), the "me" copy is bleed-through and is dropped —
    a real "them" utterance can never lose to the mic. (The old symmetric rule compared
    RMS energy and let the louder channel win, which on speakers — where the mic soaks up
    the far side loudly — mis-attributed real "them" speech to "me".)

    If the tap is silent in the window (e.g. a mic-only recording with a zeroed channel)
    there is no far-side source to anchor on, so both segments are kept. With AEC on the
    mic is already echo-free and such duplicates should be rare; the rule still runs as a
    safety net.
    """
    if not segments:
        return segments

    to_remove: set[int] = set()

    for i, seg_a in enumerate(segments):
        if i in to_remove:
            continue
        for j in range(i + 1, len(segments)):
            if j in to_remove:
                continue
            seg_b = segments[j]

            # Only compare cross-channel ("me" vs "them")
            if seg_a.speaker == seg_b.speaker:
                continue

            # Skip if segments are too far apart (performance)
            if abs(seg_a.start - seg_b.start) > 5.0:
                continue

            if _text_similarity(seg_a.text, seg_b.text) <= 0.6:
                continue
            if _temporal_overlap(seg_a, seg_b) <= 0.3:
                continue

            # The tap is ground truth for "them". Only treat this as bleed if the tap
            # actually carried audio here — otherwise keep both.
            s = max(0, int(min(seg_a.start, seg_b.start) * sr))
            e = min(len(loopback_audio), int(max(seg_a.end, seg_b.end) * sr))
            loop_rms = np.sqrt(np.mean(loopback_audio[s:e] ** 2)) if e > s else 0.0
            if loop_rms <= 1e-4:
                continue

            # Victim is always the "me" duplicate; "them" (the tap) is never dropped.
            victim = i if seg_a.speaker == "me" else j
            to_remove.add(victim)
            log.debug(
                "Dedup: dropped bleed-through 'me' seg %d (loop_rms=%.4f, aec=%s)",
                victim,
                loop_rms,
                aec_enabled,
            )
            if victim == i:
                break  # seg_a is gone — stop comparing it

    kept = [s for i, s in enumerate(segments) if i not in to_remove]
    if to_remove:
        log.info(
            "Cross-channel dedup: removed %d/%d bleed-through segments",
            len(to_remove),
            len(segments),
        )
    return kept


def _transcribe_sequential(
    mic_audio: np.ndarray,
    loopback_audio: np.ndarray,
    mic_has: bool,
    loop_has: bool,
    config: WhisperConfig,
) -> list[DiarizedSegment]:
    """Transcribe each channel sequentially."""
    results: list[DiarizedSegment] = []
    if mic_has:
        log.info("Transcribing mic channel (me)...")
        for seg in transcribe_audio(mic_audio, config):
            results.append(DiarizedSegment(speaker="me", start=seg.start, end=seg.end, text=seg.text, language=seg.language))
    if loop_has:
        log.info("Transcribing loopback channel (them)...")
        for seg in transcribe_audio(loopback_audio, config):
            results.append(DiarizedSegment(speaker="them", start=seg.start, end=seg.end, text=seg.text, language=seg.language))
    return results


def _transcribe_parallel(
    mic_audio: np.ndarray,
    loopback_audio: np.ndarray,
    config: WhisperConfig,
) -> list[DiarizedSegment]:
    """Transcribe both channels concurrently. May fail if model is not thread-safe."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[DiarizedSegment] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(transcribe_audio, mic_audio, config): "me",
            ex.submit(transcribe_audio, loopback_audio, config): "them",
        }
        for f in as_completed(futures):
            speaker = futures[f]
            for seg in f.result():
                results.append(DiarizedSegment(speaker=speaker, start=seg.start, end=seg.end, text=seg.text, language=seg.language))
    return results


def transcribe_stereo(wav_path: Path, config: WhisperConfig, aec_enabled: bool = False) -> list[DiarizedSegment]:
    """Transcribe a 2-channel WAV with channel-based speaker diarization.

    Channel 0 = mic (me), Channel 1 = loopback (them).
    Returns segments sorted by start time, with cross-channel duplicates removed.
    ``aec_enabled`` records whether the mic was echo-cancelled at capture time (forwarded
    to the dedup safety net; bleed duplicates should be rare when True).
    """
    mic_audio, loopback_audio, sr = load_stereo_wav(wav_path)
    log.info("Loaded stereo WAV: %.1fs, sr=%d", len(mic_audio) / sr, sr)

    mic_has = _has_speech(mic_audio, sr)
    loop_has = _has_speech(loopback_audio, sr)

    # Try parallel transcription, fall back to sequential if model isn't thread-safe
    if mic_has and loop_has:
        try:
            log.info("Transcribing both channels in parallel...")
            results = _transcribe_parallel(mic_audio, loopback_audio, config)
        except Exception:
            log.warning("Parallel transcription failed, falling back to sequential", exc_info=True)
            results = _transcribe_sequential(mic_audio, loopback_audio, mic_has, loop_has, config)
    else:
        results = _transcribe_sequential(mic_audio, loopback_audio, mic_has, loop_has, config)

    # Sort by start time for natural conversation flow
    results.sort(key=lambda s: s.start)

    # Cross-channel deduplication — remove bleed-through duplicates
    results = _deduplicate_cross_channel(results, mic_audio, loopback_audio, sr, aec_enabled)

    log.info(
        "Diarization complete: %d segments (me=%d, them=%d)",
        len(results),
        sum(1 for s in results if s.speaker == "me"),
        sum(1 for s in results if s.speaker == "them"),
    )
    return results
