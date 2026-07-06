"""Tests for the shared post-transcription fixups (apply_fixups) and the vocab SSOT merge."""
from __future__ import annotations

import json

from meetflow.config import Config, apply_vocab_ssot
from meetflow.text import apply_fixups

FIXUPS = [["fair flow", "VerverFlow"], ["hout calc", "HoutCalc"]]
FIXUPS_BRAND = [["houtcalc", "HoutCalc"], ["ververflow", "VerverFlow"]]


def test_both_boundary_fixup():
    assert apply_fixups("my opinion about fair flow then", FIXUPS, []) == "my opinion about VerverFlow then"


def test_both_boundary_does_not_corrupt_longer_word():
    # 'fair flowing' must NOT match 'fair flow' — the boundary AFTER the match fails.
    assert apply_fixups("a fair flowing river", FIXUPS, []) == "a fair flowing river"


def test_brand_fires_inside_compound():
    assert apply_fixups("de houtcalcfacturen", [], FIXUPS_BRAND) == "de HoutCalcfacturen"


def test_brand_standalone():
    assert apply_fixups("ververflow rocks", [], FIXUPS_BRAND) == "VerverFlow rocks"


def test_case_insensitive():
    assert apply_fixups("Fair Flow is great", FIXUPS, []) == "VerverFlow is great"


def test_empty_and_noop():
    assert apply_fixups("", FIXUPS, FIXUPS_BRAND) == ""
    assert apply_fixups("niets te fixen hier", [], []) == "niets te fixen hier"


def test_idempotent_on_correct_brand():
    # An already-correct 'VerverFlow' passes through the brand fixup unchanged.
    assert apply_fixups("VerverFlow", [], FIXUPS_BRAND) == "VerverFlow"


def test_apply_vocab_ssot_merges(tmp_path):
    (tmp_path / "vocab.json").write_text(json.dumps({
        "terms": ["HoutCalc", "VerverFlow"],
        "fixups": [["fair flow", "VerverFlow"]],
        "fixups_brand": [["houtcalc", "HoutCalc"]],
    }))
    (tmp_path / "vocab.local.json").write_text(json.dumps({"terms": ["Burg"], "fixups": [], "fixups_brand": []}))
    cfg = Config()
    apply_vocab_ssot(cfg, vocab_dir=tmp_path)
    assert "HoutCalc" in cfg.whisper.glossary and "Burg" in cfg.whisper.glossary
    assert ["fair flow", "VerverFlow"] in cfg.whisper.fixups
    assert ["houtcalc", "HoutCalc"] in cfg.whisper.fixups_brand


def test_apply_vocab_ssot_tolerates_missing(tmp_path):
    cfg = Config()
    apply_vocab_ssot(cfg, vocab_dir=tmp_path)  # no files present
    assert cfg.whisper.glossary == []
    assert cfg.whisper.fixups == []
