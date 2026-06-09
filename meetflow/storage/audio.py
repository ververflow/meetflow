"""Audio format conversion — WAV to Opus via ffmpeg."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def wav_to_opus(wav_path: Path, bitrate: str = "32k", delete_wav: bool = True) -> Path:
    """Transcode WAV to Opus. Returns the Opus file path."""
    opus_path = wav_path.with_suffix(".opus")

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(wav_path),
        "-c:a", "libopus",
        "-b:a", bitrate,
        # Preserve source channels (stereo me/them stays stereo so re-processing keeps
        # the two speakers; a mono source stays mono).
        "-ar", "16000",
        str(opus_path),
    ]

    log.info("Transcoding %s -> %s (bitrate=%s)", wav_path.name, opus_path.name, bitrate)
    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW (Windows only)
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=creationflags)

    if result.returncode != 0:
        log.error("ffmpeg failed: %s", result.stderr)
        raise RuntimeError(f"ffmpeg transcoding failed: {result.stderr[:200]}")

    opus_size_mb = opus_path.stat().st_size / (1024 * 1024)
    log.info("Opus file: %.2f MB", opus_size_mb)

    if delete_wav:
        if opus_path.stat().st_size > 0:
            wav_path.unlink()
            log.info("Deleted source WAV")
        else:
            log.error("Opus file is empty — keeping WAV as backup")

    return opus_path
