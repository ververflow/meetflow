"""Tests for config loading."""
from __future__ import annotations

from pathlib import Path

from meetflow.config import Config, load_config


def test_default_config():
    cfg = Config()
    assert cfg.audio.sample_rate == 16_000
    assert cfg.whisper.model_size == "large-v3-turbo"
    assert cfg.whisper.device == "cpu"  # macOS reuses the whisper.cpp server; no CUDA
    assert cfg.whisper.server_url == "http://127.0.0.1:8771"
    assert cfg.extraction.provider == "claude-code"
    assert cfg.privacy.retention_days == 365
    # Capture (Phase 4) defaults: per-platform auto backend, no sidecar wired in.
    assert cfg.capture.backend == "auto"
    assert cfg.capture.sidecar_path == ""
    assert cfg.capture.aec == "auto"
    assert cfg.capture.mute_behavior == "unmuted"


def test_load_config_from_toml():
    toml_path = Path(__file__).parent.parent / "meetflow.toml"
    cfg = load_config(toml_path)
    assert cfg.audio.sample_rate == 16_000
    # The committed template uses the placeholder name; local users override
    # this in meetflow.local.toml.
    assert cfg.my_name == "Your Name"
    assert cfg.whisper.backend == "cli"
    assert cfg.whisper.vad_enabled is True
    assert cfg.whisper.vad_threshold == 0.5
    assert cfg.extraction.claude_model == "sonnet"
    # CRM is commented out in the template, so it should be disabled by default.
    assert cfg.crm.enabled is False
    assert cfg.crm.client_base is None
    assert cfg.whisper.languages == ["nl", "en"]
    assert "nl" in cfg.whisper.context_prompts
    # [capture] is parsed and unknown keys are filtered by __dataclass_fields__.
    assert cfg.capture.backend == "auto"
    assert cfg.capture.route_auto_detect is True
    assert cfg.capture.tap_timeout == 15.0


def test_load_config_missing_file():
    cfg = load_config(Path("/nonexistent/meetflow.toml"))
    assert cfg.audio.sample_rate == 16_000


def test_template_has_calendar_hygiene_glossary():
    toml_path = Path(__file__).parent.parent / "meetflow.toml"
    cfg = load_config(toml_path)
    # Calendar is opt-in (off in the committed template).
    assert cfg.calendar.enabled is False
    assert cfg.calendar.match_tolerance_minutes == 20
    # Hygiene quarantine is on by default.
    assert cfg.hygiene.enabled is True
    assert cfg.hygiene.min_duration_seconds == 120
    assert cfg.hygiene.quarantine_dirname == "_quarantine"
    # Glossary is empty in the template (local users fill it).
    assert cfg.whisper.glossary == []


def test_calendar_hygiene_roundtrip(tmp_path):
    p = tmp_path / "meetflow.toml"
    p.write_text(
        '[general]\nmy_name = "X"\n'
        '[whisper]\nglossary = ["Oer Sterk", "Burg"]\n'
        '[calendar]\nenabled = true\nmy_emails = ["a@b.com"]\n'
        '[calendar.domain_slugs]\n"oersterk.nl" = "oersterk"\n'
        '[hygiene]\nmin_duration_seconds = 90\ntest_phrases = ["foo"]\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.whisper.glossary == ["Oer Sterk", "Burg"]
    assert cfg.calendar.enabled is True
    assert cfg.calendar.my_emails == ["a@b.com"]
    assert cfg.calendar.domain_slugs == {"oersterk.nl": "oersterk"}
    assert cfg.hygiene.min_duration_seconds == 90
    assert cfg.hygiene.test_phrases == ["foo"]
