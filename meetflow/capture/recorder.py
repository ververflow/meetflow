"""Dual-stream recorder — coordinates mic + loopback into a 2-channel WAV."""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from meetflow.capture.loopback import LoopbackStream
from meetflow.capture.mic import MicStream
from meetflow.config import Config
from meetflow.notify import notify

log = logging.getLogger(__name__)


class Recorder:
    """Records mic (channel 0) and system audio (channel 1) simultaneously."""

    def __init__(self, config: Config):
        self.config = config
        # AEC decision: "on" routes the mic through the sidecar's Voice-Processing path (clean
        # "me" on speakers); "off" keeps the mic in Python (the reliable Phase-3 path) and uses
        # the sidecar for the system-audio tap only. This degrades to a clean mic-only recording
        # if the tap is unavailable.
        self._resolved_aec = self._resolve_aec()
        self._mac_sidecar = (
            sys.platform == "darwin"
            and config.capture.backend in ("auto", "coreaudio")
            and self._resolved_aec == "on"
        )
        self.mic = MicStream(
            sample_rate=config.audio.sample_rate,
            device=config.audio.mic_device,
        )
        self.loopback = LoopbackStream(
            sample_rate=config.audio.sample_rate,
            capture_config=config.capture,
            data_dir=config.data_dir,
            capture_mic=self._mac_sidecar,
            resolved_aec=self._resolved_aec,
        )

    def _resolve_aec(self) -> str:
        """Resolve the effective AEC mode to "on" (dual mic+tap sidecar) or "off" (tap-only).

        "auto" + route_auto_detect probes the sidecar for the output route and turns AEC on only
        for built-in speakers. The probe SELF-GATES: with the current/older sidecar (no
        --route-json) it returns None, so "auto" stays "off" (the proven tap-only path) until a
        rebuilt sidecar is installed.
        """
        cfg = self.config.capture
        if cfg.aec == "on":
            return "on"
        if cfg.aec == "off":
            return "off"
        if cfg.route_auto_detect and sys.platform == "darwin":
            route = LoopbackStream.detect_route(cfg.sidecar_path)
            if route and route.get("wantsAEC"):
                log.info("route_auto_detect: %s -> AEC on", route.get("route"))
                return "on"
        return "off"
        self._using_sidecar_mic = False
        self._start_time: float | None = None

    def start(self) -> None:
        """Start recording both channels."""
        self._start_time = time.time()

        if self.config.privacy.auto_notify_reminder:
            log.info("HERINNERING: Meld aan de deelnemer dat dit gesprek wordt opgenomen.")
            notify("Opname gestart", "Meld de deelnemer dat dit gesprek wordt opgenomen.")

        self.loopback.start()
        # The sidecar owns the mic only if it actually started; otherwise fall back to the
        # local mic so we never lose "me" when the sidecar is unavailable.
        self._using_sidecar_mic = self._mac_sidecar and self.loopback._active
        if not self._using_sidecar_mic:
            self.mic.start()
        log.info("Recording started")

    def stop(self) -> Path | None:
        """Stop recording and write a 2-channel WAV file. Returns the WAV path."""
        loopback_audio = self.loopback.stop()
        if self._using_sidecar_mic:
            mic_audio = self.loopback.mic_audio
            if mic_audio is None:
                mic_audio = np.array([], dtype=np.float32)
        else:
            mic_audio = self.mic.stop()
        self._using_sidecar_mic = False

        if len(mic_audio) == 0 and len(loopback_audio) == 0:
            log.info("No audio captured.")
            return None

        # Align lengths — pad the shorter one with silence
        max_len = max(len(mic_audio), len(loopback_audio))
        if len(mic_audio) < max_len:
            mic_audio = np.pad(mic_audio, (0, max_len - len(mic_audio)))
        if len(loopback_audio) < max_len:
            loopback_audio = np.pad(loopback_audio, (0, max_len - len(loopback_audio)))

        # Stack into 2-channel array: ch0=mic (me), ch1=loopback (them)
        stereo = np.stack([mic_audio, loopback_audio], axis=1)

        duration = max_len / self.config.audio.sample_rate
        start_dt = datetime.fromtimestamp(self._start_time) if self._start_time else datetime.now()

        # Build output path — self-describing, sortable: YYYY-MM-DD_HHMM (+ counter on collision)
        base_name = start_dt.strftime("%Y-%m-%d_%H%M")
        meeting_dir = self.config.data_dir / "meetings" / base_name
        counter = 1
        while meeting_dir.exists():
            meeting_dir = self.config.data_dir / "meetings" / f"{base_name}_{counter}"
            counter += 1
        meeting_dir.mkdir(parents=True, exist_ok=True)
        wav_path = meeting_dir / "recording.wav"

        sf.write(str(wav_path), stereo, self.config.audio.sample_rate)
        log.info("Saved %.1fs recording to %s", duration, wav_path)

        self._start_time = None
        return wav_path

    @property
    def is_recording(self) -> bool:
        if self._start_time is None:
            return False
        if self._using_sidecar_mic:
            return self.loopback._active
        return self.mic._stream is not None

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    @property
    def max_seconds(self) -> float:
        return self.config.audio.max_duration_minutes * 60
