"""Microphone capture stream via sounddevice."""
from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


class MicStream:
    """Captures audio from the default microphone input at 16kHz mono."""

    def __init__(self, sample_rate: int = 16_000, device: str | int | None = None):
        self.sample_rate = sample_rate
        self.device = None if device == "default" else device
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    def _callback(self, indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
        if status:
            log.warning("Mic audio status: %s", status)
        with self._lock:
            self._frames.append(indata[:, 0].copy())

    def start(self) -> None:
        """Open the mic stream and begin capturing."""
        self._frames.clear()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        log.info("Mic capture started (device=%s, rate=%d)", self.device or "default", self.sample_rate)

    def stop(self) -> np.ndarray:
        """Stop capturing and return all recorded audio as a 1D float32 array."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        with self._lock:
            if not self._frames:
                return np.array([], dtype=np.float32)
            audio = np.concatenate(self._frames)
            self._frames.clear()

        log.info("Mic capture stopped: %.1fs audio", len(audio) / self.sample_rate)
        return audio
