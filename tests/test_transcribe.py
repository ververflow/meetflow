"""Tests for transcription filters and diarization."""
from __future__ import annotations

import numpy as np

from meetflow.transcribe.filters import is_low_confidence, strip_hallucinations


def test_strip_known_hallucinations():
    text = "Hello thank you for watching this is a test"
    result = strip_hallucinations(text)
    assert "thank you for watching" not in result
    assert "Hello" in result
    assert "this is a test" in result


def test_strip_nl_hallucinations():
    text = "Dat klopt ondertiteling door NOS"
    result = strip_hallucinations(text)
    assert "ondertiteling door" not in result
    assert "Dat klopt" in result


def test_strip_repeated_phrases():
    text = "Dit is een test. Dit is een test. Dit is een test. Dit is een test."
    result = strip_hallucinations(text)
    # Should reduce to 1-2 occurrences, not 4
    assert result.count("Dit is een test") < 4


def test_no_hallucination_passthrough():
    text = "We bespreken de website volgende week."
    assert strip_hallucinations(text) == text


def test_is_low_confidence():
    assert is_low_confidence(0.8, -1.5) is True
    assert is_low_confidence(0.3, -0.5) is False
    assert is_low_confidence(0.8, -0.5) is False
    assert is_low_confidence(0.3, -1.5) is False


def test_diarize_load_stereo_wav(tmp_path):
    """Test loading a 2-channel WAV file."""
    import soundfile as sf

    from meetflow.transcribe.diarize import load_stereo_wav

    sr = 16_000
    duration = 1.0
    samples = int(sr * duration)
    mic = np.random.randn(samples).astype(np.float32) * 0.1
    loopback = np.random.randn(samples).astype(np.float32) * 0.1
    stereo = np.stack([mic, loopback], axis=1)

    wav_path = tmp_path / "test.wav"
    sf.write(str(wav_path), stereo, sr)

    mic_out, loopback_out, sr_out = load_stereo_wav(wav_path)
    assert sr_out == sr
    assert len(mic_out) == samples
    assert len(loopback_out) == samples
    assert mic_out.dtype == np.float32


def test_diarize_load_mono_wav(tmp_path):
    """Test loading a mono WAV — should treat as mic-only."""
    import soundfile as sf

    from meetflow.transcribe.diarize import load_stereo_wav

    sr = 16_000
    mono = np.random.randn(sr).astype(np.float32) * 0.1

    wav_path = tmp_path / "mono.wav"
    sf.write(str(wav_path), mono, sr)

    mic_out, loopback_out, sr_out = load_stereo_wav(wav_path)
    assert sr_out == sr
    assert len(mic_out) == sr
    assert np.all(loopback_out == 0)


def test_dedup_keeps_them_over_bleed_through():
    """Far-side speech that bled loudly into the mic must stay attributed to 'them'.

    This is the Phase 4 bug fix: the mic is LOUDER than the tap (the speakers case),
    which the old symmetric RMS rule resolved in the mic's favour, mis-labelling the
    far side as 'me'. The tap is ground truth — 'them' must win.
    """
    from meetflow.transcribe.diarize import DiarizedSegment, _deduplicate_cross_channel

    sr = 16_000
    audio_len = 3 * sr
    mic_audio = np.zeros(audio_len, dtype=np.float32)
    loop_audio = np.zeros(audio_len, dtype=np.float32)
    mic_audio[sr : 2 * sr] = 0.5  # mic bleed is louder...
    loop_audio[sr : 2 * sr] = 0.1  # ...than the actual tap, but the tap still wins

    them = DiarizedSegment(speaker="them", start=1.0, end=2.0, text="we should ship on friday", language="en")
    me = DiarizedSegment(speaker="me", start=1.0, end=2.0, text="we should ship on friday", language="en")

    kept = _deduplicate_cross_channel([them, me], mic_audio, loop_audio, sr)
    assert len(kept) == 1
    assert kept[0].speaker == "them"


def test_dedup_keeps_both_when_tap_silent():
    """A mic-only recording (zeroed tap channel) has no far-side anchor — keep both."""
    from meetflow.transcribe.diarize import DiarizedSegment, _deduplicate_cross_channel

    sr = 16_000
    audio_len = 3 * sr
    mic_audio = np.zeros(audio_len, dtype=np.float32)
    mic_audio[sr : 2 * sr] = 0.5
    loop_audio = np.zeros(audio_len, dtype=np.float32)  # tap silent

    them = DiarizedSegment(speaker="them", start=1.0, end=2.0, text="same words here please", language="en")
    me = DiarizedSegment(speaker="me", start=1.0, end=2.0, text="same words here please", language="en")

    kept = _deduplicate_cross_channel([them, me], mic_audio, loop_audio, sr)
    assert len(kept) == 2


def test_dedup_preserves_distinct_overlapping_speech():
    """Both speakers genuinely talking (different words) must not be deduped."""
    from meetflow.transcribe.diarize import DiarizedSegment, _deduplicate_cross_channel

    sr = 16_000
    audio_len = 3 * sr
    mic_audio = np.full(audio_len, 0.3, dtype=np.float32)
    loop_audio = np.full(audio_len, 0.3, dtype=np.float32)

    me = DiarizedSegment(speaker="me", start=1.0, end=2.0, text="what do you think about the budget", language="en")
    them = DiarizedSegment(speaker="them", start=1.0, end=2.0, text="totally unrelated sentence over here", language="en")

    kept = _deduplicate_cross_channel([me, them], mic_audio, loop_audio, sr)
    assert len(kept) == 2
