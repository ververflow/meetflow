"""Tests for extraction schema and prompts."""
from __future__ import annotations

from meetflow.extract.prompts import build_extraction_prompt, format_transcript_for_prompt
from meetflow.extract.schema import ActionItem, ActionItems, Extraction, Meeting, Participants


def test_extraction_schema_defaults():
    e = Extraction(summary="Test meeting")
    assert e.summary == "Test meeting"
    assert e.client_needs == []
    assert e.action_items.i_owe_them == []
    assert e.action_items.they_owe_me == []


def test_extraction_schema_full():
    e = Extraction(
        summary="Besproken: website en SEO",
        client_needs=["Betere SEO", "Nieuwe foto's"],
        action_items=ActionItems(
            i_owe_them=[ActionItem(what="SEO audit", deadline="2026-04-20")],
            they_owe_me=[ActionItem(what="Foto's aanleveren")],
        ),
    )
    assert len(e.action_items.i_owe_them) == 1
    assert e.action_items.i_owe_them[0].status == "open"
    assert e.action_items.they_owe_me[0].deadline is None


def test_meeting_model():
    m = Meeting(
        id="2026-04-13T141500_test",
        client_slug="test",
        date="2026-04-13",
        start_time="14:15:00",
        end_time="14:52:00",
        duration_seconds=2220,
        language="nl",
        participants=Participants(me="Alice", them="Test"),
    )
    assert m.id == "2026-04-13T141500_test"
    assert m.transcript == []


def test_format_transcript():
    segments = [
        {"speaker": "me", "start": 0.0, "end": 3.0, "text": "Hallo, hoe gaat het?"},
        {"speaker": "them", "start": 3.5, "end": 7.0, "text": "Goed, dank je."},
    ]
    result = format_transcript_for_prompt(segments)
    assert "[0s] Me: Hallo, hoe gaat het?" in result
    assert "[4s] Them: Goed, dank je." in result


def test_build_extraction_prompt():
    result = build_extraction_prompt("test transcript", "Bedrijf: Test BV")
    assert "test transcript" in result
    assert "Test BV" in result


def test_build_extraction_prompt_no_context():
    result = build_extraction_prompt("test transcript")
    assert "test transcript" in result
