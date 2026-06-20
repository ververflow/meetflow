"""Load MeetFlow configuration from TOML."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "meetflow.toml"
_LOCAL_CONFIG = _REPO_ROOT / "meetflow.local.toml"


@dataclass
class AudioConfig:
    sample_rate: int = 16_000
    mic_device: str = "default"
    loopback_device: str = "default"
    max_duration_minutes: int = 120


@dataclass
class CaptureConfig:
    """System-audio ("them") capture backend. Phase 4 on macOS = a CoreAudio process-tap
    sidecar; Windows still uses the in-process WASAPI loopback."""

    # "auto" picks the per-platform default (coreaudio-tap on macOS, wasapi on Windows);
    # set explicitly to "coreaudio", "wasapi", or "off" to override.
    backend: str = "auto"
    # Absolute path to the compiled `meetflow-capture` sidecar (macOS). Empty → the
    # macOS path degrades to a silent "them" channel (mic-only), as before Phase 4.
    sidecar_path: str = ""  # set in meetflow.local.toml
    # Acoustic echo cancellation on the mic (4b): "auto" (on for built-in speakers,
    # off for headphones/external), "on", or "off".
    aec: str = "auto"
    route_auto_detect: bool = True  # detect speakers vs headphones to drive `aec = "auto"`
    tap_timeout: float = 15.0  # seconds to wait for the sidecar to flush + exit on stop
    mute_behavior: str = "unmuted"  # the tap must NOT silence the call the user is hearing


@dataclass
class WhisperConfig:
    # Backend: "cli" (dedicated whisper-cli + VAD per meeting — the macOS default) or
    # "server" (POST to the resident whisper.cpp dictation server; short clips / fallback).
    backend: str = "cli"
    # --- cli backend ---
    cli_path: str = "/opt/homebrew/bin/whisper-cli"
    model_path: str = ""  # set in meetflow.local.toml (e.g. ~/.local/share/whisper-models/ggml-large-v3.bin)
    best_of: int = 5
    no_speech_threshold: float = 0.50
    vad_enabled: bool = True
    vad_model: str = ""  # Silero ggml model; set in local.toml
    vad_max_speech_s: int = 30  # auto-split monologues so memory stays bounded on long meetings
    # --- server backend ---
    server_url: str = "http://127.0.0.1:8771"
    # --- legacy faster-whisper knobs (unused; kept for config compatibility) ---
    model_size: str = "large-v3-turbo"
    device: str = "cpu"
    compute_type: str = "int8"
    # --- shared decode / vad params ---
    beam_size: int = 5
    languages: list[str] = field(default_factory=lambda: ["nl", "en"])
    default_language: str | None = "auto"
    vad_threshold: float = 0.5
    vad_min_speech_ms: int = 250
    vad_min_silence_ms: int = 100
    vad_speech_pad_ms: int = 200
    context_prompts: dict[str, str] = field(default_factory=lambda: {
        "nl": "Hé, even een update over het project. Kunnen we dat morgen bespreken?",
        "en": "Hey, quick update on the project. Can we discuss this tomorrow?",
    })
    # Static proper nouns appended to the whisper --prompt so names/companies transcribe
    # correctly (e.g. ["Oer Sterk", "Burg", "HoutCalc", "Dani Verver"]). Per-meeting names
    # (calendar attendees, CRM contact) are layered on top at runtime; see engine.build_prompt.
    glossary: list[str] = field(default_factory=list)


@dataclass
class ExtractionConfig:
    provider: str = "claude-code"
    claude_model: str = "haiku"
    ollama_model: str = "llama3"
    ollama_url: str = "http://localhost:11434"


@dataclass
class StorageConfig:
    archive_format: str = "opus"
    opus_bitrate: str = "32k"
    keep_wav: bool = False


@dataclass
class PrivacyConfig:
    auto_notify_reminder: bool = True
    retention_days: int = 365


@dataclass
class CRMConfig:
    enabled: bool = True
    client_base: Path | None = None  # None = no CRM integration
    profile_path: str = "crm/profile.json"  # Relative to client dir
    activity_field: str = "activiteiten"
    notes_field: str = "notities"


@dataclass
class CalendarConfig:
    """Google Calendar enrichment via the local `gws` CLI. Read-only: only event metadata
    (title, attendees, time) is read; recordings/transcripts never leave the machine."""

    enabled: bool = False  # opt-in; off in the committed template
    gws_path: str = "gws"  # resolved on PATH by default
    calendar_id: str = "primary"
    match_tolerance_minutes: int = 20  # the hotkey start may lag the scheduled start
    lookback_minutes: int = 15  # query-window padding before the recording start
    lookahead_minutes: int = 15  # and after the recording end
    timeout_seconds: float = 12.0
    my_emails: list[str] = field(default_factory=list)  # own addresses, excluded from "them"
    domain_slugs: dict[str, str] = field(default_factory=dict)  # attendee email domain -> slug


@dataclass
class HygieneConfig:
    """Auto-quarantine of test/junk recordings. Quarantine = tag + move (reversible); NEVER
    deletes."""

    enabled: bool = True
    min_duration_seconds: int = 120  # shorter recordings are quarantine candidates
    min_transcript_words: int = 15  # near-empty transcript => quarantine candidate
    test_phrases: list[str] = field(
        default_factory=lambda: ["hello hello test", "test test test", "een twee drie test"]
    )
    quarantine_dirname: str = "_quarantine"
    quarantine_tag: str = "test"


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    my_name: str = ""
    language: str = "auto"  # "auto", "nl", "en", "de", etc.
    crm: CRMConfig = field(default_factory=CRMConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    hygiene: HygieneConfig = field(default_factory=HygieneConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML. If meetflow.local.toml exists it overrides
    meetflow.toml (so you can keep personal settings out of git)."""
    if path is None:
        path = _LOCAL_CONFIG if _LOCAL_CONFIG.exists() else _DEFAULT_CONFIG
    if not path.exists():
        return Config()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general", {})

    # Build CRM config
    crm_raw = raw.get("crm", general)  # Support both [crm] section and legacy [general] fields
    crm_base = crm_raw.get("client_base")
    crm_config = CRMConfig(
        enabled=crm_raw.get("crm_enabled", crm_base is not None),
        client_base=Path(crm_base) if crm_base else None,
        profile_path=crm_raw.get("profile_path", "crm/profile.json"),
        activity_field=crm_raw.get("activity_field", "activiteiten"),
        notes_field=crm_raw.get("notes_field", "notities"),
    )

    # Build Whisper config — handle context_prompts as nested table
    whisper_raw = dict(raw.get("whisper", {}))
    context_prompts = whisper_raw.pop("context_prompts", None)
    whisper_kwargs = {k: v for k, v in whisper_raw.items() if k in WhisperConfig.__dataclass_fields__}
    if context_prompts:
        whisper_kwargs["context_prompts"] = context_prompts

    cfg = Config(
        data_dir=Path(general.get("data_dir", "./data")),
        my_name=general.get("my_name", ""),
        language=general.get("language", "auto"),
        crm=crm_config,
        calendar=CalendarConfig(
            **{k: v for k, v in raw.get("calendar", {}).items() if k in CalendarConfig.__dataclass_fields__}
        ),
        hygiene=HygieneConfig(
            **{k: v for k, v in raw.get("hygiene", {}).items() if k in HygieneConfig.__dataclass_fields__}
        ),
        audio=AudioConfig(**{k: v for k, v in raw.get("audio", {}).items() if k in AudioConfig.__dataclass_fields__}),
        capture=CaptureConfig(**{k: v for k, v in raw.get("capture", {}).items() if k in CaptureConfig.__dataclass_fields__}),
        whisper=WhisperConfig(**whisper_kwargs),
        extraction=ExtractionConfig(
            **{k: v for k, v in raw.get("extraction", {}).items() if k in ExtractionConfig.__dataclass_fields__}
        ),
        storage=StorageConfig(
            **{k: v for k, v in raw.get("storage", {}).items() if k in StorageConfig.__dataclass_fields__}
        ),
        privacy=PrivacyConfig(
            **{k: v for k, v in raw.get("privacy", {}).items() if k in PrivacyConfig.__dataclass_fields__}
        ),
    )
    # Resolve data_dir relative to config file
    if not cfg.data_dir.is_absolute():
        cfg.data_dir = (path.parent / cfg.data_dir).resolve()
    return cfg
