"""Tests for the consecutive-loop collapse filter and the anti-loop decode knobs.

The fixture tests/fixtures/journal_loops.json is the REAL transcript of the 2026-07-02 solo
journal that first exposed the problem: one sentence emitted 17x across two consecutive runs
(length 3 and 14). The collapse filter is the deterministic backstop; the -mc/-et params (proven
via the offline A/B on that recording) are the upstream fix.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from meetflow.config import WhisperConfig
from meetflow.transcribe.engine import _CliBackend
from meetflow.transcribe.filters import _norm, collapse_repeated_segments

_FIXTURE = Path(__file__).parent / "fixtures" / "journal_loops.json"


@dataclass
class _Seg:
    """Minimal stand-in for engine.Segment (collapse only needs .start/.end/.text)."""

    start: float
    end: float
    text: str


def _load_fixture() -> list[_Seg]:
    data = json.loads(_FIXTURE.read_text())
    return [_Seg(start=s["start"], end=s["end"], text=s["text"]) for s in data]


def _dominant_loop(segs: list[_Seg]) -> tuple[str, int]:
    """The most-repeated normalized sentence in the fixture, and its count (the loop)."""
    text, count = Counter(_norm(s.text) for s in segs).most_common(1)[0]
    return text, count


# ── the real journal loop ────────────────────────────────────────────────────────


def test_journal_loop_collapses():
    segs = _load_fixture()
    looped, before = _dominant_loop(segs)  # the real fixture: 17 copies across two runs
    assert before >= 15, f"fixture drifted: expected the ~17x loop, got {before}"

    out = collapse_repeated_segments(segs)
    after = sum(1 for s in out if _norm(s.text) == looped)

    # The copies sat in two consecutive runs (len 3 + 14) → each collapses to one.
    assert after <= 2, f"loop not collapsed: {after} copies remain (was {before})"
    assert len(out) < len(segs), "collapse removed nothing"
    # The unique real content (the opening) must survive untouched.
    assert any(s.text.startswith("Hey Cloud") for s in out)


def test_collapsed_segment_covers_the_span():
    segs = _load_fixture()
    looped, _ = _dominant_loop(segs)
    out = collapse_repeated_segments(segs)
    # The kept copy of a collapsed run extends its end over the whole looped span.
    kept = [s for s in out if _norm(s.text) == looped]
    assert kept and all(s.end >= s.start for s in kept)


# ── synthetic: the two triggers, and the deliberate non-trigger ──────────────────


def test_consecutive_run_of_three_collapses():
    segs = [
        _Seg(0.0, 1.0, "same line"),
        _Seg(10.0, 11.0, "same line"),
        _Seg(20.0, 21.0, "same line"),  # spread 20s > collision, but run==3 → collapse
    ]
    out = collapse_repeated_segments(segs)
    assert len(out) == 1
    assert out[0].start == 0.0 and out[0].end == 21.0


def test_timestamp_collision_pair_collapses():
    segs = [
        _Seg(203.0, 205.0, "looped"),
        _Seg(203.1, 205.5, "looped"),  # run==2 but starts within 1.0s → collapse
    ]
    out = collapse_repeated_segments(segs)
    assert len(out) == 1


def test_spread_pair_is_left_alone():
    # A length-2 verbatim repeat far apart in time is NOT a loop; keep both.
    segs = [
        _Seg(0.0, 1.0, "ja"),
        _Seg(30.0, 31.0, "ja"),
    ]
    out = collapse_repeated_segments(segs)
    assert len(out) == 2


def test_clean_transcript_passthrough():
    segs = [
        _Seg(0.0, 2.0, "eerste zin"),
        _Seg(2.0, 4.0, "tweede zin"),
        _Seg(4.0, 6.0, "derde zin"),
    ]
    out = collapse_repeated_segments(segs)
    assert [s.text for s in out] == ["eerste zin", "tweede zin", "derde zin"]


def test_empty_passthrough():
    assert collapse_repeated_segments([]) == []


# ── the decode command stays byte-identical for meetings by default ──────────────


def _cmd(cfg: WhisperConfig) -> list[str]:
    return _CliBackend()._build_cmd(cfg, Path("/tmp/x.wav"), Path("/tmp/out"), "nl")


def test_meeting_command_byte_identical_by_default():
    cmd = _cmd(WhisperConfig())
    assert "-mc" not in cmd, "default config must not emit -mc (would change the meeting command)"
    assert "-et" not in cmd, "default config must not emit -et"


def test_journal_params_emit_flags():
    cmd = _cmd(WhisperConfig(max_context=0, entropy_thold=2.8))
    assert cmd[cmd.index("-mc") + 1] == "0"
    assert cmd[cmd.index("-et") + 1] == "2.8"
