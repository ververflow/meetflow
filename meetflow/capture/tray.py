"""System tray icon for recording status."""
from __future__ import annotations

import logging
import threading

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

# Icon size
_SIZE = 64


def _create_icon(color: str) -> Image.Image:
    """Create a simple colored circle icon."""
    img = Image.new("RGBA", (_SIZE, _SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse([margin, margin, _SIZE - margin, _SIZE - margin], fill=color)
    return img


def _idle_icon() -> Image.Image:
    return _create_icon("#666666")


def _recording_icon() -> Image.Image:
    return _create_icon("#FF0000")


class TrayIcon:
    """System tray icon showing MeetFlow recording status."""

    def __init__(self) -> None:
        self._icon = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Show the tray icon in idle state."""
        try:
            import pystray

            self._icon = pystray.Icon(
                "meetflow",
                _idle_icon(),
                "MeetFlow — Idle",
                menu=pystray.Menu(
                    pystray.MenuItem("MeetFlow", lambda: None, enabled=False),
                    pystray.MenuItem("Quit", self._quit),
                ),
            )
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
            log.info("Tray icon started")
        except Exception:
            log.warning("Could not start tray icon (pystray not available or no display)")

    def set_recording(self, client_slug: str = "") -> None:
        """Switch icon to recording state (red)."""
        if self._icon is None:
            return
        self._icon.icon = _recording_icon()
        self._icon.title = f"MeetFlow — Recording ({client_slug})" if client_slug else "MeetFlow — Recording"

    def set_idle(self) -> None:
        """Switch icon to idle state (gray)."""
        if self._icon is None:
            return
        self._icon.icon = _idle_icon()
        self._icon.title = "MeetFlow — Idle"

    def set_processing(self) -> None:
        """Switch icon to processing state."""
        if self._icon is None:
            return
        self._icon.title = "MeetFlow — Processing..."

    def stop(self) -> None:
        """Remove the tray icon."""
        if self._icon is not None:
            self._icon.stop()
            self._icon = None
            log.info("Tray icon stopped")

    def _quit(self) -> None:
        self.stop()
