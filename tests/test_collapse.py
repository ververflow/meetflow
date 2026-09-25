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
import numpy as np

from meetflow.transcribe.filters import _norm, collapse_repeated_segments, find_loops

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


# ── the anti-loop decode knobs ──────────────────────────────────────────────────


def _cmd(cfg: WhisperConfig) -> list[str]:
    return _CliBackend()._build_cmd(cfg, Path("/tmp/x.wav"), Path("/tmp/out"), "nl")


def test_meetings_decode_without_carried_context_by_default():
    # 2026-09-24: with the model-default context an hour-long call looped one sentence 1530x.
    cmd = _cmd(WhisperConfig())
    assert cmd[cmd.index("-mc") + 1] == "0"
    assert "-et" not in cmd, "default config must not emit -et"


def test_model_default_context_emits_no_flag():
    assert "-mc" not in _cmd(WhisperConfig(max_context=-1))


def test_retry_command_drops_the_prompt():
    cfg = WhisperConfig(glossary=["HoutCalc"])
    assert "--prompt" in _CliBackend()._build_cmd(cfg, Path("/tmp/x.wav"), Path("/tmp/out"), "nl")
    assert "--prompt" not in _CliBackend()._build_cmd(cfg, Path("/tmp/x.wav"), Path("/tmp/out"), "nl", prompt=False)


def test_journal_params_emit_flags():
    cmd = _cmd(WhisperConfig(max_context=0, entropy_thold=2.8))
    assert cmd[cmd.index("-mc") + 1] == "0"
    assert cmd[cmd.index("-et") + 1] == "2.8"


# ── long loops: found, re-decoded, and marked when they survive ─────────────────


def _loop(start: float, n: int, text: str = "Je moet echt op de hoogte gaan.") -> list[_Seg]:
    return [_Seg(start + i, start + i + 1, text) for i in range(n)]


def test_find_loops_reports_long_runs_only():
    segs = [_Seg(0, 1, "a"), *_loop(1, 3, "ja."), *_loop(10, 25), _Seg(40, 41, "b")]
    assert find_loops(segs) == [(10, 35, 25)]


def test_collapse_marks_the_survivor_of_a_loop():
    from meetflow.transcribe.engine import Segment

    segs = [Segment(i, i + 1, "zelfde zin hier.", "nl") for i in range(30)]
    out = collapse_repeated_segments(segs)
    assert len(out) == 1 and out[0].looped == 30 and out[0].end == 30


def test_redecode_splices_the_fresh_window_in():
    from meetflow.transcribe.engine import SAMPLE_RATE, Segment

    backend = _CliBackend()
    seen = {}

    def fake_decode(audio, config, language, prompt=True):
        seen.update(n=len(audio), prompt=prompt, mc=config.max_context, et=config.entropy_thold)
        return [Segment(0.5, 2.0, "wat er echt gezegd werd", "nl")]

    backend._decode = fake_decode
    audio = np.zeros(100 * SAMPLE_RATE, dtype=np.float32)
    segs = [Segment(1, 2, "voor", "nl")] + [Segment(10 + i, 11 + i, "lus.", "nl") for i in range(25)] + [Segment(50, 51, "na", "nl")]
    out = backend._redecode_loops(audio, segs, WhisperConfig(), "nl")
    assert [s.text for s in out] == ["voor", "wat er echt gezegd werd", "na"]
    assert out[1].start == 10.5
    assert seen["prompt"] is False and seen["mc"] == 0 and seen["et"]


def test_meeting_md_warns_about_gaps(tmp_path):
    from meetflow.extract.schema import Meeting, Participants
    from meetflow.notify import gap_note
    from meetflow.storage.files import save_meeting_markdown

    m = Meeting(id="x", client_slug="rob", date="2026-09-24", start_time="19:14", end_time="20:19",
                duration_seconds=3940, language="nl", participants=Participants(me="Dani"),
                transcript_gaps=[[1200.0, 2940.0]])
    md = save_meeting_markdown(m, tmp_path).read_text()
    assert "Transcript onvolledig" in md and "20:00-49:00" in md
    assert "29 min" in gap_note(m)
    m.transcript_gaps = []
    assert gap_note(m) == ""
