"""Tests for the whisper prompt builder (glossary + per-meeting vocabulary)."""
from __future__ import annotations

from meetflow.config import WhisperConfig
from meetflow.transcribe.engine import build_prompt, set_meeting_vocab


def test_build_prompt_base_only():
    cfg = WhisperConfig()
    set_meeting_vocab([])
    p = build_prompt(cfg, "nl")
    assert "Eigennamen" not in p
    assert p == cfg.context_prompts["nl"]


def test_build_prompt_glossary():
    cfg = WhisperConfig(glossary=["Oer Sterk", "Burg"])
    set_meeting_vocab([])
    p = build_prompt(cfg, "nl")
    assert "Eigennamen" in p
    assert "Oer Sterk" in p and "Burg" in p


def test_build_prompt_meeting_vocab_and_clear():
    cfg = WhisperConfig(glossary=["Burg"])
    set_meeting_vocab(["Niels Koning"])
    p = build_prompt(cfg, "nl")
    assert "Niels Koning" in p and "Burg" in p
    set_meeting_vocab([])
    assert "Niels Koning" not in build_prompt(cfg, "nl")


def test_build_prompt_dedup():
    cfg = WhisperConfig(glossary=["Burg"])
    set_meeting_vocab(["Burg", "Burg"])
    p = build_prompt(cfg, "nl")
    assert p.count("Burg") == 1
    set_meeting_vocab([])


def test_build_prompt_budget_trims():
    cfg = WhisperConfig(glossary=[f"Name{i}" for i in range(500)])
    set_meeting_vocab([])
    p = build_prompt(cfg, "nl")
    assert len(p) <= 700
