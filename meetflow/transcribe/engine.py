"""Whisper transcription engine — pluggable backend.

Two backends share one public API (transcribe_audio/load_model/get_model/Segment):

- ``cli``  (default for meetings): a DEDICATED ``whisper-cli`` invocation per channel with
  Silero VAD. Runs in its own process, so a long meeting never blocks the resident
  dictation whisper-server; VAD strips silence (killing the silence-hallucinations); failure
  is isolated to one channel. Same large-v3 model file on disk, Metal-accelerated.
- ``server``: POST each channel to the always-on whisper.cpp server (127.0.0.1:8771). Kept
  for short clips / as a fallback. faster-whisper is intentionally NOT used (no Metal on
  Apple Silicon).

diarize.py and filters.py are unchanged: the CLI backend serializes its subprocess calls
with an internal lock (so diarize's parallel path can't launch two model-loading processes),
and both backends return the same ``Segment`` list with start/end + text + language.
"""
from __future__ import annotations

import io
import json
import logging
import math
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from meetflow.config import WhisperConfig
from meetflow.transcribe.filters import is_low_confidence, strip_hallucinations

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

# whisper reports languages by full English name (server) or ISO code (cli); normalize both.
_LANG_NAME_TO_CODE = {"dutch": "nl", "english": "en", "nl": "nl", "en": "en"}


@dataclass
class Segment:
    """A single transcribed segment with timing."""

    start: float
    end: float
    text: str
    language: str


# ─── shared helpers ────────────────────────────────────────────────────────────


def _audio_to_wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf.read()


def _resolve_label(detected: str | None, language: str | None, allowed: list[str]) -> str:
    """Decide the nl/en label for a segment, constrained to the configured languages."""
    allowed = allowed or ["nl"]
    if language in allowed:
        return language
    code = _LANG_NAME_TO_CODE.get((detected or "").lower(), (detected or "").lower())
    return code if code in allowed else allowed[0]


# Per-meeting proper-noun vocabulary, set by the pipeline before transcription and cleared
# after (so it never leaks into the next meeting via the module-global backend).
_MEETING_VOCAB: list[str] = []
_PROMPT_CHAR_BUDGET = 700  # keep the combined --prompt well under large-v3's ~224-token cap


def set_meeting_vocab(terms: list[str] | None) -> None:
    """Set the per-meeting proper-noun vocab appended to the whisper prompt. Clear with []."""
    global _MEETING_VOCAB
    _MEETING_VOCAB = [t.strip() for t in (terms or []) if t and t.strip()]


def build_prompt(config: WhisperConfig, language: str | None, vocab: list[str] | None = None) -> str:
    """Build the whisper --prompt: base context hint + proper-noun vocabulary.

    Vocabulary = config.glossary (static) plus per-meeting names (calendar attendees, CRM
    contact). Trims from the end of the vocab list if the combined prompt exceeds the budget,
    so the static glossary and the earliest (most relevant) names survive.
    """
    base = config.context_prompts.get(language) or config.context_prompts.get("nl", "")
    extra = vocab if vocab is not None else _MEETING_VOCAB
    terms = list(dict.fromkeys(list(getattr(config, "glossary", []) or []) + list(extra)))
    if not terms:
        return base

    def _combine(ts: list[str]) -> str:
        vocab_str = ("Eigennamen: " + ", ".join(ts) + ".") if ts else ""
        return (base + " " + vocab_str).strip() if base else vocab_str

    combined = _combine(terms)
    while terms and len(combined) > _PROMPT_CHAR_BUDGET:
        terms.pop()
        combined = _combine(terms)
    return combined


# ─── server backend (HTTP /inference) ──────────────────────────────────────────


class _ServerBackend:
    def __init__(self) -> None:
        self.url: str | None = None

    def ensure_ready(self, config: WhisperConfig) -> None:
        url = getattr(config, "server_url", "http://127.0.0.1:8771").rstrip("/")
        try:
            httpx.get(url + "/", timeout=5.0)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"whisper-server not reachable at {url} ({e}). "
                "Is the com.ververflow.whisper-server LaunchAgent running?"
            ) from e
        self.url = url
        log.info("whisper-server reachable at %s", url)

    def transcribe(self, audio: np.ndarray, config: WhisperConfig, language: str | None) -> list[Segment]:
        if self.url is None:
            self.ensure_ready(config)
        data = {"response_format": "verbose_json", "temperature": "0.0", "language": language or "auto"}
        prompt = build_prompt(config, language)
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("channel.wav", _audio_to_wav_bytes(audio), "audio/wav")}
        resp = httpx.post(self.url + "/inference", data=data, files=files, timeout=600.0)
        resp.raise_for_status()
        payload = resp.json()

        probs = payload.get("language_probabilities") or {}
        allowed = config.languages or ["nl"]
        if language in allowed:
            label = language
        elif any(probs.get(lang, 0) > 0 for lang in allowed):
            label = max(allowed, key=lambda lang: probs.get(lang, 0.0))
        else:
            label = _resolve_label(payload.get("detected_language") or payload.get("language"), language, allowed)

        results: list[Segment] = []
        for seg in payload.get("segments", []):
            if is_low_confidence(float(seg.get("no_speech_prob", 0.0)), float(seg.get("avg_logprob", 0.0))):
                continue
            text = strip_hallucinations((seg.get("text") or "").strip())
            if text:
                results.append(Segment(start=float(seg["start"]), end=float(seg["end"]), text=text, language=label))
        log.info("Transcribed %d segments in %s via whisper-server", len(results), label)
        return results


# ─── cli backend (dedicated whisper-cli + VAD) ─────────────────────────────────


class _CliBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()  # serialize subprocesses → one model-loading job at a time

    def ensure_ready(self, config: WhisperConfig) -> None:
        cli = getattr(config, "cli_path", "/opt/homebrew/bin/whisper-cli")
        model = getattr(config, "model_path", "")
        if not Path(cli).exists():
            raise RuntimeError(f"whisper-cli not found at {cli}. Set [whisper].cli_path.")
        if not model or not Path(model).expanduser().exists():
            raise RuntimeError(f"whisper model not found at {model!r}. Set [whisper].model_path.")
        if getattr(config, "vad_enabled", True):
            vm = getattr(config, "vad_model", "")
            if not vm or not Path(vm).expanduser().exists():
                raise RuntimeError(f"VAD enabled but model not found at {vm!r}. Set [whisper].vad_model or vad_enabled=false.")
        log.info("whisper-cli backend ready (%s, vad=%s)", cli, getattr(config, "vad_enabled", True))

    def _build_cmd(self, config: WhisperConfig, wav: Path, out_base: Path, language: str | None) -> list[str]:
        cmd = [
            getattr(config, "cli_path", "/opt/homebrew/bin/whisper-cli"),
            "-m", str(Path(getattr(config, "model_path", "")).expanduser()),
            "-f", str(wav),
            "-l", language or getattr(config, "default_language", None) or "auto",
            "-bs", str(config.beam_size),
            "-bo", str(getattr(config, "best_of", 5)),
            "-fa", "-sns",
            "-nth", str(getattr(config, "no_speech_threshold", 0.50)),
            "--carry-initial-prompt",
            "-oj", "-ojf",
            "-of", str(out_base),
        ]
        prompt = build_prompt(config, language)
        if prompt:
            cmd += ["--prompt", prompt]
        if getattr(config, "vad_enabled", True):
            cmd += [
                "--vad",
                "-vm", str(Path(getattr(config, "vad_model", "")).expanduser()),
                "-vt", str(config.vad_threshold),
                "-vspd", str(config.vad_min_speech_ms),
                "-vsd", str(config.vad_min_silence_ms),
                "-vmsd", str(getattr(config, "vad_max_speech_s", 30)),
                "-vp", str(config.vad_speech_pad_ms),
            ]
        return cmd

    @staticmethod
    def _avg_logprob(tokens: list[dict]) -> float:
        """Mean log-probability over content tokens (special [_*] tokens excluded)."""
        lps = [
            math.log(max(float(t["p"]), 1e-9))
            for t in tokens
            if isinstance(t.get("p"), (int, float)) and not (t.get("text") or "").startswith("[_")
        ]
        return sum(lps) / len(lps) if lps else 0.0

    def transcribe(self, audio: np.ndarray, config: WhisperConfig, language: str | None) -> list[Segment]:
        timeout = max(600, int(len(audio) / SAMPLE_RATE * 1.5) + 120)
        with self._lock, tempfile.TemporaryDirectory(prefix="meetflow-") as tmp:
            wav = Path(tmp) / "channel.wav"
            out_base = Path(tmp) / "out"
            sf.write(str(wav), audio, SAMPLE_RATE, subtype="PCM_16")
            cmd = self._build_cmd(config, wav, out_base, language)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0:
                raise RuntimeError(f"whisper-cli failed ({proc.returncode}): {proc.stderr[-300:]}")
            payload = json.loads((out_base.with_suffix(".json")).read_text())

        detected = (payload.get("result") or {}).get("language")
        label = _resolve_label(detected, language, config.languages or ["nl"])

        results: list[Segment] = []
        for seg in payload.get("transcription", []):
            offsets = seg.get("offsets") or {}
            avg_logprob = self._avg_logprob(seg.get("tokens") or [])
            # no_speech_prob is not emitted by whisper-cli; VAD already removed silence.
            if is_low_confidence(0.0, avg_logprob):
                continue
            text = strip_hallucinations((seg.get("text") or "").strip())
            if text:
                results.append(
                    Segment(
                        start=float(offsets.get("from", 0)) / 1000.0,
                        end=float(offsets.get("to", 0)) / 1000.0,
                        text=text,
                        language=label,
                    )
                )
        log.info("Transcribed %d segments in %s via whisper-cli (vad=%s)", len(results), label, getattr(config, "vad_enabled", True))
        return results


# ─── dispatch / public API (unchanged contract) ───────────────────────────────

_backend: _ServerBackend | _CliBackend | None = None
_backend_kind: str | None = None


def _get_backend(config: WhisperConfig):
    global _backend, _backend_kind
    kind = getattr(config, "backend", "cli")
    if _backend is None or _backend_kind != kind:
        _backend = _CliBackend() if kind == "cli" else _ServerBackend()
        _backend_kind = kind
        _backend.ensure_ready(config)
    return _backend


def load_model(config: WhisperConfig) -> None:
    """Initialize + verify the configured backend. No heavy in-process model load."""
    _get_backend(config)


def get_model():
    """Return the ready backend, or raise if load_model() hasn't run."""
    if _backend is None:
        raise RuntimeError("Transcription backend not initialized. Call load_model() first.")
    return _backend


def detect_language(audio: np.ndarray, config: WhisperConfig) -> str:
    """Best-effort nl/en detection (kept for API compatibility)."""
    segs = _get_backend(config).transcribe(audio, config, None)
    return segs[0].language if segs else (config.languages or ["nl"])[0]


def transcribe_audio(audio: np.ndarray, config: WhisperConfig, language: str | None = None) -> list[Segment]:
    """Transcribe a 1D float32 16kHz array via the configured backend. Returns timed segments."""
    if audio is None or len(audio) == 0:
        return []
    return _get_backend(config).transcribe(audio, config, language)
