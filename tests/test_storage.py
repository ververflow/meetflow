"""Tests for storage layer."""
from __future__ import annotations

from meetflow.extract.schema import ActionItem, ActionItems, Extraction, Meeting, Participants, TranscriptSegment
from meetflow.storage.database import MeetingDB
from meetflow.storage.files import save_meeting_json, save_meeting_markdown


def _make_meeting() -> Meeting:
    return Meeting(
        id="2026-04-13T141500_test-client",
        client_slug="test-client",
        date="2026-04-13",
        start_time="14:15:00",
        end_time="14:52:00",
        duration_seconds=2220,
        language="nl",
        participants=Participants(me="Alice", them="Nick"),
        transcript=[
            TranscriptSegment(speaker="them", start=0.0, end=3.4, text="Hé Alice, goed dat je er bent."),
            TranscriptSegment(speaker="me", start=3.5, end=7.1, text="Ja, thanks Nick."),
        ],
        extraction=Extraction(
            summary="Test meeting over website.",
            client_needs=["Nieuwe website"],
            action_items=ActionItems(
                i_owe_them=[ActionItem(what="Design opleveren", deadline="2026-04-20")],
                they_owe_me=[ActionItem(what="Content aanleveren")],
            ),
        ),
    )


def test_save_meeting_json(tmp_path):
    meeting = _make_meeting()
    path = save_meeting_json(meeting, tmp_path)
    assert path.exists()
    assert path.name == "meeting.json"

    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["client_slug"] == "test-client"
    assert data["extraction"]["summary"] == "Test meeting over website."


def test_save_meeting_markdown(tmp_path):
    meeting = _make_meeting()
    path = save_meeting_markdown(meeting, tmp_path)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "test-client" in content
    assert "Design opleveren" in content
    assert "Content aanleveren" in content
    assert "## Action Items" in content


def test_database_index_and_search(tmp_path):
    db = MeetingDB(tmp_path / "test.db")
    meeting = _make_meeting()
    db.index_meeting(meeting, "meeting.json", "recording.opus")

    # Search for transcript content
    results = db.search("Alice")
    assert len(results) >= 1
    assert results[0]["client_slug"] == "test-client"

    db.close()


def test_database_actions(tmp_path):
    db = MeetingDB(tmp_path / "test.db")
    meeting = _make_meeting()
    db.index_meeting(meeting, "meeting.json")

    actions = db.get_open_actions()
    assert len(actions) == 2

    actions_client = db.get_open_actions("test-client")
    assert len(actions_client) == 2

    actions_empty = db.get_open_actions("nonexistent")
    assert len(actions_empty) == 0

    db.close()


def test_database_client_history(tmp_path):
    db = MeetingDB(tmp_path / "test.db")
    meeting = _make_meeting()
    db.index_meeting(meeting, "meeting.json")

    history = db.get_meetings_by_client("test-client")
    assert len(history) == 1
    assert history[0]["summary"] == "Test meeting over website."

    db.close()
