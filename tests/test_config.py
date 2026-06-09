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
