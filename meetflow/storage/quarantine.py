"""Auto-quarantine of test/junk recordings.

Quarantine is REVERSIBLE: the meeting folder is tagged and moved under
`meetings/_quarantine/`. Nothing is ever deleted automatically.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from meetflow.config import HygieneConfig
from meetflow.extract.schema import Meeting

log = logging.getLogger(__name__)


def is_junk(meeting: Meeting, cfg: HygieneConfig) -> tuple[bool, str]:
    """Return (is_junk, reason). Pure; no side effects.

    Junk = too short, near-empty transcript, or matches a known test phrase.
    """
    if not cfg.enabled:
        return False, ""
    if meeting.duration_seconds < cfg.min_duration_seconds:
        return True, f"duration {meeting.duration_seconds}s < {cfg.min_duration_seconds}s"
    text = " ".join(seg.text for seg in meeting.transcript).strip()
    words = text.split()
    if len(words) < cfg.min_transcript_words:
        return True, f"transcript has {len(words)} words < {cfg.min_transcript_words}"
    low = text.lower()
    for phrase in cfg.test_phrases:
        if phrase.lower() in low:
            return True, f"matched test phrase '{phrase}'"
    return False, ""


def quarantine_meeting(meeting_dir: Path, data_dir: Path, cfg: HygieneConfig) -> Path:
    """Move the meeting folder under meetings/_quarantine/. Returns the new path.

    Reversible (a plain move). If a folder with the same name already exists in quarantine,
    the existing one is left and the source is merged in.
    """
    qroot = data_dir / "meetings" / cfg.quarantine_dirname
    qroot.mkdir(parents=True, exist_ok=True)
    dst = qroot / meeting_dir.name
    if dst.exists():
        # Already quarantined under this name; copy any missing files, then drop the source.
        for item in meeting_dir.iterdir():
            target = dst / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
        shutil.rmtree(str(meeting_dir), ignore_errors=True)
    else:
        shutil.move(str(meeting_dir), str(dst))
    log.info("Quarantined meeting %s -> %s", meeting_dir.name, dst)
    return dst
