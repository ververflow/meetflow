"""Double-tap Right Ctrl hotkey — copied from whisper-hotkey mechanics.

Uses pynput + persistent OutputStream beep, identical approach to whisper-hotkey
but on Right Ctrl with distinct double-beep tones.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np
import sounddevice as sd
from pynput.keyboard import Key, Listener

log = logging.getLogger(__name__)

# Hotkey config (same values as whisper-hotkey)
DOUBLE_TAP_WINDOW = 0.4
CTRL_HOLD_MAX = 0.3
DEBOUNCE = 1.0
BEEP_DELAY = 0.12

# Persistent audio stream for beeps (same approach as whisper-hotkey)
_beep_stream = None


def beep_start() -> None:
    """Double rising beep — MeetFlow recording ON."""
    _beep(600, 80)
    _beep(900, 80)


def beep_stop() -> None:
    """Double falling beep — MeetFlow recording OFF."""
    _beep(900, 80)
    _beep(500, 120)


def beep_ready() -> None:
    """Single tone — MeetFlow ready."""
    _beep(500, 80)


def beep_done() -> None:
    """Three quick high beeps — processing complete, saved."""
    for _ in range(3):
        _beep(1000, 60)
        time.sleep(0.02)


def beep_error() -> None:
    """Slow low tone — something went wrong."""
    _beep(300, 300)


def _beep(freq: int, ms: int) -> None:
    """Play a single tone via persistent OutputStream."""
    global _beep_stream
    sr = 44_100
    if _beep_stream is None or _beep_stream.closed:
        _beep_stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
        _beep_stream.start()
    n = int(sr * ms / 1000)
    t = np.linspace(0, ms / 1000, n, dtype=np.float32)
    wave = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    _beep_stream.write(wave.reshape(-1, 1))
    time.sleep(ms / 1000 + 0.02)


# State (same pattern as whisper-hotkey)
_last_ctrl_tap = 0.0
_ctrl_down_time = 0.0
_any_key_while_ctrl = False
_ctrl_held = False
_last_trigger_time = 0.0
_on_toggle_fn = None


def _on_double_ctrl() -> None:
    global _last_trigger_time
    now = time.time()
    if now - _last_trigger_time < DEBOUNCE:
        return
    _last_trigger_time = now

    if _on_toggle_fn is not None:
        threading.Thread(target=_on_toggle_fn, daemon=True).start()


def _on_press(key) -> None:
    global _any_key_while_ctrl, _ctrl_down_time, _ctrl_held
    if key == Key.ctrl_r:
        _ctrl_held = True
        _ctrl_down_time = time.time()
        _any_key_while_ctrl = False
    elif _ctrl_held:
        _any_key_while_ctrl = True


def _on_release(key) -> None:
    global _last_ctrl_tap, _ctrl_held
    if key != Key.ctrl_r:
        return

    _ctrl_held = False

    if _any_key_while_ctrl:
        return
    if time.time() - _ctrl_down_time > CTRL_HOLD_MAX:
        return
    if time.time() - _last_trigger_time < DEBOUNCE:
        _last_ctrl_tap = 0.0
        return

    now = time.time()
    if now - _last_ctrl_tap < DOUBLE_TAP_WINDOW:
        _last_ctrl_tap = 0.0
        _on_double_ctrl()
    else:
        _last_ctrl_tap = now


class HotkeyListener:
    """Listens for double-tap Right Ctrl to toggle recording."""

    def __init__(self, on_toggle) -> None:
        global _on_toggle_fn
        _on_toggle_fn = on_toggle
        self._listener: Listener | None = None

    def start(self) -> None:
        self._listener = Listener(on_press=_on_press, on_release=_on_release)
        self._listener.start()
        log.info("Hotkey listener active (double-tap Right Ctrl to toggle)")

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
