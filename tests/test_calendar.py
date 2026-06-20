"""Tests for Google Calendar enrichment (matching + graceful degradation)."""
from __future__ import annotations

import meetflow.integrations.calendar as cal_mod
from meetflow.config import CalendarConfig
from meetflow.integrations.calendar import find_event, get_calendar_context


def _solo_events():
    # Solo events (no attendees) — the common case where the slug must come from the title.
    return [
        {
            "summary": "Burg sales call",
            "start": {"dateTime": "2026-05-19T10:30:00+02:00"},
            "end": {"dateTime": "2026-05-19T11:15:00+02:00"},
        },
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-05-19T09:00:00+02:00"},
            "end": {"dateTime": "2026-05-19T09:15:00+02:00"},
        },
    ]


def test_find_event_matches_overlapping(monkeypatch):
    cfg = CalendarConfig(enabled=True)
    monkeypatch.setattr(cal_mod, "_query_events", lambda c, a, b: _solo_events())
    m = find_event(cfg, "2026-05-19", "10:28:59", 2072)
    assert m is not None
    assert m.title == "Burg sales call"
    assert m.slug_hint == "burg"  # derived from the summary (no attendees)
    assert m.has_real_attendees is False
    assert "Burg sales call" in get_calendar_context(cfg, m)


def test_find_event_uses_attendees_and_domain_slug(monkeypatch):
    cfg = CalendarConfig(
        enabled=True,
        my_emails=["dani@ververflow.com"],
        domain_slugs={"oersterk.nl": "oersterk"},
    )
    events = [{
        "summary": "Webshop bespreking",
        "start": {"dateTime": "2026-06-19T11:00:00+02:00"},
        "end": {"dateTime": "2026-06-19T11:45:00+02:00"},
        "attendees": [
            {"email": "dani@ververflow.com", "self": True},
            {"email": "jan@oersterk.nl", "displayName": "Jan"},
        ],
    }]
    monkeypatch.setattr(cal_mod, "_query_events", lambda c, a, b: events)
    m = find_event(cfg, "2026-06-19", "11:04:00", 2475)
    assert m.them_names == ["Jan"]
    assert m.has_real_attendees is True
    assert m.slug_hint == "oersterk"  # domain map wins over summary/name
    assert m.attendee_emails == ["jan@oersterk.nl"]


def test_find_event_no_overlap(monkeypatch):
    cfg = CalendarConfig(enabled=True)
    monkeypatch.setattr(cal_mod, "_query_events", lambda c, a, b: _solo_events())
    m = find_event(cfg, "2026-05-19", "20:00:00", 600)
    assert m is None


def test_find_event_disabled():
    assert find_event(CalendarConfig(enabled=False), "2026-05-19", "10:30:00", 600) is None


def test_find_event_gws_failure():
    # A non-existent binary makes _query_events raise; find_event must swallow it -> None.
    cfg = CalendarConfig(enabled=True, gws_path="definitely-not-a-binary-xyz")
    assert find_event(cfg, "2026-05-19", "10:28:59", 2072) is None


def test_find_event_skips_all_day(monkeypatch):
    cfg = CalendarConfig(enabled=True)
    events = [{"summary": "Vakantie", "start": {"date": "2026-05-19"}, "end": {"date": "2026-05-20"}}]
    monkeypatch.setattr(cal_mod, "_query_events", lambda c, a, b: events)
    assert find_event(cfg, "2026-05-19", "10:00:00", 600) is None


def test_get_calendar_context_empty():
    assert get_calendar_context(CalendarConfig(enabled=True), None) == ""
