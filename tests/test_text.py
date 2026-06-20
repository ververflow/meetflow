"""Tests for the shared text helpers."""
from __future__ import annotations

from meetflow.text import clean_event_summary, normalize_dashes, sanitize_slug, slug_from_summary


def test_sanitize_slug():
    assert sanitize_slug("Oer Sterk!!") == "oer-sterk"
    assert sanitize_slug("  Burg  ") == "burg"
    assert sanitize_slug("") == "unknown"


def test_normalize_dashes():
    assert normalize_dashes("Niels — Burg") == "Niels - Burg"
    assert normalize_dashes("a–b") == "a-b"
    assert normalize_dashes("plain") == "plain"


def test_clean_event_summary():
    assert clean_event_summary("Call Niels Koning - Burg") == "Niels Koning - Burg"
    assert clean_event_summary("Meeting met Frank") == "Frank"
    assert clean_event_summary("Standup") == "Standup"


def test_slug_from_summary():
    assert slug_from_summary("Burg sales call") == "burg"
    assert slug_from_summary("Call Niels Koning - Burg") == "niels"
    assert slug_from_summary("") == ""
