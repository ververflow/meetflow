"""Tests for junk/test auto-quarantine (detection + reversible move)."""
from __future__ import annotations

from meetflow.config import HygieneConfig
from meetflow.extract.schema import Extraction, Meeting, Participants, TranscriptSegment
from meetflow.storage.quarantine import is_junk, quarantine_meeting


def _meeting(duration=600, text="x", mid="2026-06-20_1200") -> Meeting:
    return Meeting(
        id=mid,
        client_slug="unknown",
        date="2026-06-20",
        start_time="12:00:00",
        end_time="12:10:00",
        duration_seconds=duration,
        language="nl",
        participants=Participants(me="Dani"),
        transcript=[TranscriptSegment(speaker="me", start=0.0, end=1.0, text=text)],
        extraction=Extraction(summary="x"),
    )


def test_is_junk_short_duration():
    junk, reason = is_junk(_meeting(duration=30, text=" ".join(["woord"] * 20)), HygieneConfig())
    assert junk and "duration" in reason


def test_is_junk_few_words():
    junk, reason = is_junk(_meeting(duration=600, text="hoi daar"), HygieneConfig())
    assert junk and "words" in reason


def test_is_junk_test_phrase():
    text = "hello hello test " + " ".join(["woord"] * 20)
    junk, reason = is_junk(_meeting(duration=600, text=text), HygieneConfig())
    assert junk and "test phrase" in reason


def test_not_junk_real_meeting():
    text = "we bespraken het project en de planning uitgebreid en concreet vandaag " * 2
    junk, _ = is_junk(_meeting(duration=600, text=text), HygieneConfig())
    assert not junk


def test_is_junk_disabled():
    junk, _ = is_junk(_meeting(duration=1, text=""), HygieneConfig(enabled=False))
    assert not junk


def test_quarantine_moves_folder(tmp_path):
    cfg = HygieneConfig()
    mdir = tmp_path / "meetings" / "2026-06-20_1200"
    mdir.mkdir(parents=True)
    (mdir / "meeting.json").write_text("{}", encoding="utf-8")
    (mdir / "recording.opus").write_text("audio", encoding="utf-8")

    dst = quarantine_meeting(mdir, tmp_path, cfg)
    assert dst == tmp_path / "meetings" / "_quarantine" / "2026-06-20_1200"
    assert dst.exists()
    assert not mdir.exists()  # moved, not copied
    assert (dst / "meeting.json").exists()
    assert (dst / "recording.opus").exists()  # nothing deleted
