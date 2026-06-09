"""System-audio ("them") capture.

On Windows this uses PyAudioWPatch WASAPI loopback (in-process). On macOS there is no
built-in loopback; the clean capture is a CoreAudio process-tap, implemented as a separate
compiled Swift sidecar (`meetflow-capture`) that this module spawns per meeting and reads
back as a 16 kHz mono WAV. If the sidecar is missing or fails, the macOS path degrades
gracefully to an empty "them" channel so the recorder still produces a valid (mic-only)
recording — diarize.py already handles a silent loopback channel.

The LoopbackStream interface (``__init__(sample_rate)`` / ``start()`` / ``stop() -> ndarray``)
is unchanged; the CoreAudio sidecar slots behind it. ``capture_config`` / ``data_dir`` are
optional so a bare ``LoopbackStream(sample_rate=...)`` (tests, legacy callers) still works.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"


def _tail(data: bytes | None, n: int = 500) -> str:
    """Last n chars of captured sidecar stderr, for diagnostics."""
    if not data:
        return ""
    return data.decode("utf-8", "replace").strip()[-n:]


class LoopbackStream:
    """Captures system audio output at 16kHz mono. Empty channel on macOS until the tap lands."""

    def __init__(self, sample_rate: int = 16_000, capture_config=None, data_dir: Path | None = None,
                 capture_mic: bool = False):
        self.sample_rate = sample_rate
        self._capture_config = capture_config
        self._data_dir = data_dir
        # On macOS the sidecar can also capture the (AEC-cleaned) mic, so "me" and "them"
        # share one process and stay aligned. When True, mic_audio holds "me" after stop().
        self._capture_mic = capture_mic
        self.mic_audio: np.ndarray | None = None
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None
        self._pyaudio = None
        self._active = False
        self._backend = "off"
        # macOS CoreAudio sidecar state
        self._sidecar_pid: int | None = None
        self._tap_dir: Path | None = None
        self._tap_wav: Path | None = None
        self._mic_wav: Path | None = None
        self._status_path: Path | None = None

    def _resolve_backend(self) -> str:
        """Pick the capture backend: "coreaudio", "wasapi", or "off"."""
        backend = self._capture_config.backend if self._capture_config else "auto"
        if backend == "auto":
            if _IS_MACOS:
                return "coreaudio"
            if _IS_WINDOWS:
                return "wasapi"
            return "off"
        return backend

    def start(self) -> None:
        self._backend = self._resolve_backend()
        if self._backend == "wasapi" and _IS_WINDOWS:
            self._start_wasapi()
        elif self._backend == "coreaudio" and _IS_MACOS:
            self._start_coreaudio()
        else:
            if self._backend != "off":
                log.warning("System-audio backend %r unavailable on this platform; recording mic-only.", self._backend)
            else:
                log.info("System-audio capture disabled (backend=off); recording mic-only.")
            self._active = False

    def stop(self) -> np.ndarray:
        if not self._active:
            return np.array([], dtype=np.float32)
        if self._backend == "wasapi":
            return self._stop_wasapi()
        if self._backend == "coreaudio":
            return self._stop_coreaudio()
        return np.array([], dtype=np.float32)

    # ── Windows WASAPI loopback (unchanged; replaced by the CoreAudio tap on macOS) ──

    def _start_wasapi(self) -> None:
        import pyaudiowpatch as pyaudio

        self._frames.clear()
        self._pyaudio = pyaudio.PyAudio()
        loopback = self._pyaudio.get_default_wasapi_loopback()
        self._device_channels = loopback["maxInputChannels"]
        self._device_rate = int(loopback["defaultSampleRate"])
        log.info("Loopback device: %s (channels=%d, native_rate=%d)", loopback["name"], self._device_channels, self._device_rate)
        self._stream = self._pyaudio.open(
            format=pyaudio.paFloat32,
            channels=self._device_channels,
            rate=self._device_rate,
            input=True,
            input_device_index=loopback["index"],
            frames_per_buffer=1024,
            stream_callback=self._callback,
        )
        self._stream.start_stream()
        self._active = True
        log.info("Loopback capture started")

    def _callback(self, in_data, frame_count, time_info, status_flags):
        import pyaudiowpatch as pyaudio

        if in_data is None:
            return None, pyaudio.paContinue
        audio = np.frombuffer(in_data, dtype=np.float32)
        if self._device_channels > 1:
            audio = audio.reshape(-1, self._device_channels).mean(axis=1)
        if self._device_rate != self.sample_rate:
            ratio = self.sample_rate / self._device_rate
            target_len = int(len(audio) * ratio)
            indices = np.linspace(0, len(audio) - 1, target_len)
            audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)
        with self._lock:
            self._frames.append(audio)
        return None, pyaudio.paContinue

    def _stop_wasapi(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pyaudio is not None:
            self._pyaudio.terminate()
            self._pyaudio = None
        self._active = False
        with self._lock:
            if not self._frames:
                return np.array([], dtype=np.float32)
            audio = np.concatenate(self._frames)
            self._frames.clear()
        log.info("Loopback capture stopped: %.1fs audio", len(audio) / self.sample_rate)
        return audio

    # ── macOS CoreAudio process-tap (Swift sidecar; Phase 4) ──
    #
    # The sidecar owns the realtime tap and writes a self-describing 16kHz mono WAV.
    # IPC is deliberately a file handoff (mirrors whisper-cli→json / ffmpeg→opus): no
    # streaming, restart-safe, directly inspectable. EVERY failure path returns an empty
    # array so the recorder pads "them" with silence and degrades to a mic-only recording.
    #
    # The sidecar is launched via `open` (LaunchServices), NOT a direct child process: macOS
    # attributes the system-audio TCC grant to the RESPONSIBLE process, and a child of this
    # daemon would be evaluated against the daemon (denied → silence). Launched via `open`,
    # the .app is its own responsible process and uses the bundle's grant. Because `open`
    # detaches, we stop the sidecar by the pid it writes into capture-status.json.

    def _start_coreaudio(self) -> None:
        import subprocess
        import uuid

        cfg = self._capture_config
        app = self._app_bundle(cfg.sidecar_path if cfg else "")
        if not app or not Path(app).exists():
            log.info("CoreAudio sidecar .app not found (%s); recording mic-only.", app or "unset")
            self._active = False
            return

        base = self._data_dir or Path.cwd()
        self._tap_dir = base / "control" / "tap-tmp" / uuid.uuid4().hex
        self._tap_wav = self._tap_dir / "them.wav"
        self._status_path = self._tap_dir / "capture-status.json"
        self.mic_audio = None
        args = ["--out", str(self._tap_wav), "--sample-rate", str(self.sample_rate)]
        if self._capture_mic:
            self._mic_wav = self._tap_dir / "me.wav"
            aec = cfg.aec if cfg else "auto"
            args += ["--mic-out", str(self._mic_wav), "--aec", aec]
        try:
            self._tap_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["open", "-n", app, "--args", *args],
                check=True, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            log.exception("Failed to launch CoreAudio sidecar; recording mic-only.")
            self._active = False
            self._cleanup_tap()
            return

        self._sidecar_pid = self._await_pid(timeout=10.0)
        if self._sidecar_pid is None:
            log.warning("CoreAudio sidecar did not report a pid; recording mic-only.")
            self._active = False
            self._cleanup_tap()
            return
        self._active = True
        log.info("CoreAudio tap started (pid=%d) → %s", self._sidecar_pid, self._tap_wav)

    @staticmethod
    def _app_bundle(path: str) -> str:
        """Resolve the .app bundle from either a bundle path or an inner-binary path."""
        if not path:
            return ""
        p = Path(path)
        if p.suffix == ".app":
            return str(p)
        for parent in p.parents:
            if parent.suffix == ".app":
                return str(parent)
        return str(p)

    def _await_pid(self, timeout: float) -> int | None:
        """Poll capture-status.json (written by the sidecar) for its pid."""
        import json
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = json.loads(self._status_path.read_text())
                pid = data.get("pid")
                if pid and data.get("state") in ("recording", "stopped"):
                    return int(pid)
            except (OSError, ValueError, AttributeError):
                pass
            time.sleep(0.1)
        return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        import os

        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _stop_coreaudio(self) -> np.ndarray:
        import os
        import signal
        import time

        pid, self._sidecar_pid = self._sidecar_pid, None
        self._active = False
        empty = np.array([], dtype=np.float32)
        if pid is None:
            self._cleanup_tap()
            return empty

        timeout = self._capture_config.tap_timeout if self._capture_config else 15.0
        try:
            os.kill(pid, signal.SIGTERM)  # sidecar flushes its WAV + exits cleanly
        except ProcessLookupError:
            pass
        except OSError:
            log.exception("Error signalling CoreAudio sidecar; recording mic-only.")
            self._cleanup_tap()
            return empty

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._pid_alive(pid):
            time.sleep(0.1)
        if self._pid_alive(pid):
            log.warning("CoreAudio sidecar (pid=%d) did not exit in %.0fs.", pid, timeout)

        them = self._read_wav(self._tap_wav, "them")
        if self._capture_mic:
            self.mic_audio = self._read_wav(self._mic_wav, "me")
        self._cleanup_tap()
        return them

    def _read_wav(self, wav: Path | None, label: str) -> np.ndarray:
        import soundfile as sf

        if wav is None or not wav.exists() or wav.stat().st_size == 0:
            log.info("CoreAudio sidecar produced no audio for '%s' channel.", label)
            return np.array([], dtype=np.float32)
        try:
            data, sr = sf.read(str(wav), dtype="float32")
        except Exception:
            log.exception("Failed to read %s WAV %s.", label, wav)
            return np.array([], dtype=np.float32)

        if data.ndim > 1:  # sidecar should emit mono, but downmix defensively
            data = data.mean(axis=1)
        if sr != self.sample_rate and len(data) > 1:  # safety net — sidecar already targets 16k
            ratio = self.sample_rate / sr
            target_len = int(len(data) * ratio)
            indices = np.linspace(0, len(data) - 1, target_len)
            data = np.interp(indices, np.arange(len(data)), data)
        audio = np.ascontiguousarray(data, dtype=np.float32)
        log.info("CoreAudio sidecar captured %.1fs for '%s'", len(audio) / self.sample_rate, label)
        return audio

    def _cleanup_tap(self) -> None:
        import shutil

        tap_dir, self._tap_dir = self._tap_dir, None
        self._tap_wav = None
        self._mic_wav = None
        self._status_path = None
        if tap_dir is not None:
            shutil.rmtree(tap_dir, ignore_errors=True)
