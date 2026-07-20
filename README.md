# MeetFlow

Personal meeting intelligence on macOS (Apple Silicon). Press **Ctrl+Alt+M**
to start recording, press again to stop. MeetFlow captures your mic + the call audio,
transcribes with Whisper, extracts a structured summary (summary, action items, client
needs) with Claude, and archives everything locally — full-text searchable.

Audio never leaves your Mac: capture and Whisper transcription run fully locally, with no audio
upload. The summary step sends the **transcript text** to Claude via the authenticated Claude CLI
(no API key), so the conversation text is processed in the cloud while the audio is not.

## Recording & privacy

Record only conversations you take part in, and tell the other participants you are recording.
Under Dutch law you may record a conversation you are a party to, but for any work or business use
the GDPR/AVG applies: inform participants and have a lawful basis. Because the summary step sends
the transcript to Claude (Anthropic), the conversation text leaves your machine for processing;
for business use you also need a processor agreement with Anthropic. When recording starts MeetFlow
beeps, shows a red menubar dot, and pops up an on-screen reminder to inform participants.

Lives at `~/code/tools/meetflow` (one of Dani's self-built tools). Machine wiring — the
launchd agent and the Hammerspoon trigger — lives in `~/macbook` (see "How it's wired").

> **Status:** live and in daily use. Two lanes: **meetings** (Ctrl+Alt+M, 2-channel mic+tap) and a
> solo **journal / brainstorm** lane (Hyper+J, mic-only, `kind='journal'`, distilled + stored in
> `~/journal`). Output is organized by venture/type (INDEX grouped per venture); a failed LLM step
> keeps the transcript (`meetflow redistill <id>` to re-distil). Full current state is in `CLAUDE.md`
> (the "Status" + "Capture lanes" sections). Records **two channels** for meetings — your mic
> ("me") + the system audio via a CoreAudio process-tap ("them") — transcribes, extracts, and
> archives, all locally and searchable. On speakers you'll hear a mild echo in the raw audio (the
> mic also picks up the system sound), but the transcript is de-bleeded and clean; headphones
> remove the echo. One item remains: capture-time echo-cancellation (`aec = "on"`) is implemented
> but currently broken, so keep `aec = "auto"`. See `CLAUDE.md` → "Remaining work" for the rest.

## Pipeline

```
Ctrl+Alt+M → recording starts (Tink + 🔴 menubar + consent reminder)
... conversation via Meet / Teams / phone ...
Ctrl+Alt+M → recording stops (Pop + ⏳)
  → whisper-cli transcription (dedicated process, Silero VAD, large-v3, Metal)
  → channel-based diarization + cross-channel dedup
  → Claude CLI extraction (sonnet)
  → done tone + macOS notification + Finder opens the meeting folder
```

## Stack

- Python 3.11+ (managed with `uv`)
- Transcription: **whisper.cpp** `whisper-cli` (large-v3, Metal) + **Silero VAD** — a dedicated
  process per meeting, so it never blocks the resident dictation whisper-server. A `server`
  backend (POST to `127.0.0.1:8771`) is kept as a fallback for short clips.
- Extraction: [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude -p --model sonnet`) — no API key
- SQLite + FTS5 for the searchable archive; FFmpeg for opus archiving
- Trigger + menubar: Hammerspoon (`~/macbook/hammerspoon/.hammerspoon/meetflow.lua`)
- Autostart: launchd (`~/macbook/launchd/.../com.ververflow.meetflow.plist`)

## Requirements (already met on this Mac)

- macOS 14.4+ (this Mac: 26.5), Apple Silicon
- `uv`, `ffmpeg`, `whisper-cpp` (whisper-cli + whisper-server), the `claude` CLI on PATH
- The large-v3 model at `~/.local/share/whisper-models/ggml-large-v3.bin`
- The Silero VAD model at `~/.local/share/whisper-models/ggml-silero-v6.2.0.bin`

## Setup (from scratch on a new Mac)

```sh
cd ~/code/tools/meetflow
uv venv --python 3.12
uv pip install -e ".[dev]"

# VAD model (once):
curl -L -o ~/.local/share/whisper-models/ggml-silero-v6.2.0.bin \
  https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin

# Local config (machine-specific, gitignored):
cp meetflow.toml meetflow.local.toml   # then set my_name + absolute model paths
```

### How it's wired (machine-as-code, in `~/macbook`)

```sh
# Hammerspoon trigger (Ctrl+Alt+M + menubar):
stow -d ~/macbook hammerspoon && hs -c "hs.reload()"

# launchd daemon (autostart at login, supervised):
stow -d ~/macbook launchd
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ververflow.meetflow.plist
launchctl enable   gui/$(id -u)/com.ververflow.meetflow
launchctl kickstart -k gui/$(id -u)/com.ververflow.meetflow
```

**One manual step:** the first time you record, macOS prompts for **Microphone** access for the
daemon's Python — grant it once. The system-audio tap uses a separate `kTCCServiceAudioCapture`
grant for `MeetFlowCapture.app` (already granted; survives reboots via the stable self-signed cert).

## Daily use

Just **Ctrl+Alt+M** to start, **Ctrl+Alt+M** to stop. The menubar 🎬 shows state
(🔴 recording · ⏳ processing · ⚫ daemon down). Everything lands in one fixed place;
you point Claude at the right client/recording yourself later.

## Where data lives

One fixed, local-only directory (Time-Machine backed, never synced, never in git):

```
~/Library/Application Support/MeetFlow/
  meetings/
    INDEX.md                       # auto-generated overview (date · title · duration · open actions)
    2026-06-08_1430/
      recording.opus               # audio archive (keeps source channels)
      meeting.json                 # structured data (transcript + extraction) — source of truth
      meeting.md                   # human-readable summary
  meetflow.db                      # SQLite + FTS5 search index
  control/{command,status.json}    # daemon IPC (Hammerspoon ↔ daemon)
  meetflow.pid                     # single-instance lock
```

## CLI commands

```
meetflow daemon              # the background daemon (launchd runs this; watches the control file)
meetflow start|stop|toggle   # write a command to the daemon (terminal/Raycast can trigger too)
meetflow process <wav>       # run the pipeline on an existing WAV
meetflow index               # rebuild meetings/INDEX.md
meetflow search <query>      # full-text search over transcripts
meetflow actions             # list open action items
meetflow history             # meeting overview
meetflow export <client>     # export all data for a client (AVG/GDPR)
meetflow delete <client>     # delete all data for a client (AVG/GDPR)
meetflow cleanup             # prune meetings older than retention_days
```

(`listen`/`record`/`tag`/`install`/`uninstall` remain in the code but are legacy — the daily
path is the daemon + Ctrl+Alt+M.)

## Configuration

`meetflow.toml` is the committed template; copy to `meetflow.local.toml` (gitignored) for
machine-specific overrides. Key sections:

- `[general] my_name`, `data_dir`
- `[whisper] backend = "cli"`, `model_path`, `vad_model`, `vad_*` — the transcription engine
- `[extraction] claude_model = "sonnet"` — quality over cost
- `[storage]` — opus archive settings
- `[privacy] retention_days = 365` — set per your AVG/ROPA register

## Architecture

```
capture/      mic.py        — sounddevice mic capture ("me")
              loopback.py   — system audio ("them"); WASAPI on Windows, CoreAudio process-tap
                              sidecar (MeetFlowCapture.app) on macOS — live since Phase 4a
              recorder.py   — stacks mic + loopback into a 2-channel WAV (ch0=me, ch1=them)
              hotkey.py / tray.py — legacy (pynput/pystray); replaced by Hammerspoon + the daemon

transcribe/   engine.py     — pluggable backend: CliBackend (whisper-cli + VAD) | ServerBackend (HTTP)
              diarize.py    — channel-based speaker assignment + cross-channel dedup
              filters.py    — hallucination + low-confidence filtering

extract/      llm.py        — Claude CLI driver + robust JSON-salvage parser
              prompts.py    — extraction prompt (with per-call language directive)
              schema.py     — Pydantic models (Extraction, Meeting, …)

storage/      database.py   — SQLite + FTS5; files.py — JSON+MD + INDEX.md; audio.py — opus
notify.py     — macOS notifications (osascript)
daemon.py     — control-file watcher, status.json, pidfile lock, heartbeat
cli.py        — Click commands + pipeline orchestration
config.py     — TOML loader (meetflow.local.toml overrides meetflow.toml)
```

## Tests

```sh
.venv/bin/pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
