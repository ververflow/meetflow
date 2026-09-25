"""Desktop notifications for MeetFlow.

On macOS we use `osascript` (display notification) instead of the Windows tray balloon /
PowerShell toast. set_tray() is kept as a no-op shim for API compatibility with the listen
daemon until the tray is replaced in the macOS reliability pass.
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

_tray = None


def set_tray(tray) -> None:
    """Kept for API compatibility (the macOS build does not use a pystray balloon)."""
    global _tray
    _tray = tray


def notify(title: str, message: str) -> None:
    """Show a desktop notification."""
    if sys.platform == "darwin":
        _notify_macos(title, message)
    else:
        log.info("Notification: %s — %s", title, message[:80])


def _osa_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " — ")


def _notify_macos(title: str, message: str) -> None:
    script = (
        f'display notification "{_osa_escape(message)}" '
        f'with title "MeetFlow" subtitle "{_osa_escape(title)}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        log.info("Notification: %s — %s", title, message[:80])
    except Exception as e:
        log.warning("osascript notification failed: %s", e)


def gap_note(meeting) -> str:
    """Notification line for an incomplete transcript (see Meeting.transcript_gaps); "" when complete."""
    gaps = getattr(meeting, "transcript_gaps", None)
    if not gaps:
        return ""
    minutes = max(1, round(sum(e - s for s, e in gaps) / 60))
    return f"\n⚠ transcript mist ~{minutes} min (whisper-lus): meetflow retranscribe '{meeting.id}'"
