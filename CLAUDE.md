# MeetFlow — context for Claude / contributors

See [README.md](README.md) for user-facing docs. This file captures the non-obvious
design context for anyone modifying the code (human or AI).

## What it is

Personal meeting intelligence tool. Records mic + system audio, transcribes with Whisper,
extracts a structured summary via the Claude CLI, stores everything locally with full-text search.
LIVE and in daily use on macOS (Apple M5 Pro): both channels captured (mic "me" + CoreAudio tap
"them"), transcribed, extracted, stored, searchable, survives reboots. Day-to-day path:
Ctrl+Alt+M → daemon → pipeline → archive. AEC is the only genuinely open capability (see below).

This tree lives at `~/code/tools/meetflow` (self-built tools share the `~/code/tools` repo).
Confidential meeting data lives OUTSIDE the repo at `~/Library/Application Support/MeetFlow`
(gitignored, Time-Machine only, never synced). Extraction = **Sonnet**.

## Capture lanes

Three lanes share one engine + store:
- **Meetings** (2+ people): Ctrl+Alt+M → daemon → stereo mic/tap → per-channel whisper-cli + VAD →
  diarize → Sonnet extraction → store. `kind='meeting'`.
- **Journal / brainstorm** (solo): **Hyper+J** → daemon `journal` verb → recorder `mic_only`
  (no tap, no diarization, every segment = "me") → whisper-cli (VAD, auto nl/en, `max_context=0`) →
  a JOURNAL distillation (themes/insights/decisions/open-questions/todos/notes_to_claude) →
  `kind='journal'`, stored in `<data>/journal/` + `JOURNAL.md`, viewed at `~/journal`.
  `meetflow/journal.py` is a PARALLEL pipeline that never branches into the meeting hot path.
  This is the spoken journal the brain harvests.
- **Dictation** (text-to-cursor) is a SEPARATE tool in `~/macbook` (dictation.lua + the resident
  whisper-server on :8771), NOT this repo — but it shares the vocab SSOT and runs `-l auto` + VAD.

## Output organization — orthogonal axes, not a folder tree

`client_slug` no longer conflates venture/client/journal. Recordings are tagged on four axes:
`kind` (meeting|journal) + `venture` (houtcalc | ververflow | "") + `type` (discovery |
working-session | partner-sync | product-feedback | user-interview | reflection | brainstorm) +
`client_slug` (just the counterparty). New meetings auto-set `venture` from the counterparty
(`config.venture_for`, HoutCalc-slugs → houtcalc, else agency); journals are `kind=journal`
(not a venture), default `type=reflection`. `venture`/`type` are indexed DB columns. INDEX.md is
a derived view GROUPED PER VENTURE over flat storage. Re-tag: `meetflow classify <id> --venture …
--type … --counterparty …`.

## Resilience

- **LLM step is NON-FATAL in both lanes**: on failure (e.g. Claude usage limit) the transcript is
  still saved + tagged `distillatie-mislukt`; recover with `meetflow redistill <id>` (re-distils the
  SAVED transcript — no re-record, no re-transcribe). This closes a real data-loss window (a
  Claude-limit crash AFTER transcription but BEFORE save; audio survives as opus, recoverable via
  `process --kind journal`).
- No-speech never strands orphan WAVs: meetings archive+quarantine, journals discard the silent clip.
- The daemon runs the pipeline in a BACKGROUND THREAD, so the menubar stays live during processing
  and a toggle pressed mid-processing drains the stale command instead of firing a surprise record.
- Anti-loop: `filters.collapse_repeated_segments` (deterministic backstop) + journal `max_context=0`
  (without it the journal engine can loop one sentence many times).
- FTS has AFTER DELETE/UPDATE triggers, so search never desyncs on re-index.

## CLI + config

CLI: `journal` (toggle a solo session), `redistill <id>`, `classify <id> …`, `process --kind
journal`, `index`, `classify`/`tag`, `doctor` (preflight), `backfill` (re-extract titles + repair
old mojibake + reconcile slug). Config: `[journal]` (dirname, max_context), `venture_for`, whisper
`fixups`/`fixups_brand`, `apply_vocab_ssot` (merges `~/.config/whisper/vocab.json` +
`vocab.local.json` into glossary + fixups at CLI startup — NOT in load_config, so tests keep an
empty glossary). Fixups correct the transcript-of-record ("fair flow" → VerverFlow), not just the
summary. Extraction defaults: sonnet, 48k context.

## Architecture

```
Ctrl+Alt+M (Hammerspoon meetflow.lua, ~/macbook)
  └─ writes a one-word command to <data>/control/command
       └─ meetflow daemon (launchd com.ververflow.meetflow) watches it
            ├─ capture/  recorder.py → mic.py (sounddevice "me", ch0)
            │            + loopback.py (system "them", ch1 — CoreAudio tap)
            ├─ transcribe/ engine.py — PLUGGABLE backend:
            │              CliBackend (whisper-cli + Silero VAD, default) | ServerBackend (HTTP)
            │              diarize.py (channel-based + cross-channel dedup) · filters.py
            ├─ extract/  llm.py (Claude CLI sonnet + robust JSON-salvage) · prompts · schema
            └─ storage/  database.py (SQLite+FTS5) · files.py (JSON+MD+INDEX.md) · audio.py (opus)
       └─ daemon writes <data>/control/status.json (state/heartbeat) — Hammerspoon reads it for the glyph
notify.py — macOS osascript notifications.  config.py — TOML loader (local.toml overrides toml).
```

Load-bearing design decisions:
- **Engine backend abstraction** (`transcribe/engine.py`): `transcribe_audio(audio, config,
  language) -> list[Segment]` hides whether whisper-cli or the HTTP server was used. Default
  `cli`: a DEDICATED whisper-cli per channel with Silero VAD (`ggml-silero-v6.2.0.bin`), large-v3,
  Metal — own process, so it does NOT block the resident dictation whisper-server; VAD kills
  silence-hallucinations; failure isolated per channel. `CliBackend` serializes its subprocesses
  with a `threading.Lock`, so `diarize.py`/`filters.py` need no backend knowledge. whisper-cli
  `-ojf` has no `no_speech_prob` (VAD covers it); `avg_logprob` is computed from per-token `p`.
- **Trigger ≠ work**: Hammerspoon owns only the hotkey + menubar (it already has Accessibility);
  the daemon owns capture + pipeline and needs only Microphone (+ the tap's audio-capture grant).
  IPC is a plain control file + status.json (debuggable, restart-safe). This is the fix for the old
  "sometimes doesn't start" (a pynput listener got zero events without TCC while looking alive).
- **Single instance + heartbeat**: `daemon.py` holds a `portalocker` pidfile (a second instance
  logs and exits) and logs a heartbeat every 30s into status.json, so liveness is checkable from
  `tail` and the menubar.
- **Cross-channel dedup** (`diarize._deduplicate_cross_channel`): tap-anchored asymmetric
  attribution — the tap is ground truth for "them"; a "them" segment is never dropped for a
  bleed-through "me" duplicate (kept-both when the tap is silent).

## System-audio capture (CoreAudio tap)

`swift-sidecar/` is a compiled CoreAudio process-tap app (`meetflow-capture`, bundled as
`MeetFlowCapture.app`). Global tap (`CATapDescription(stereoGlobalTapButExcludeProcesses: [])`,
`muteBehavior=.unmuted` so the call is not silenced) → private aggregate device anchored to the
default-output clock → realtime IOProc downmixes to mono into a lock-free SPSC ring buffer → a drain
thread streams a 16 kHz mono Float32 WAV. SIGTERM does ordered teardown (stop → destroy IOProc →
destroy aggregate → destroy tap → flush). Build via `swift-sidecar/build.sh` (`swift build -c
release` + assemble & codesign the `.app`, system frameworks only, no Xcode).

`capture/loopback.py` `darwin` branch launches `MeetFlowCapture.app` per meeting via `open -n …
--args --out … --sample-rate 16000`, polls `capture-status.json` for the sidecar pid, stops it with
SIGTERM, reads back `them.wav`. EVERY failure path (missing .app, open error, no pid, timeout,
missing/empty WAV) returns an empty array → mic-only degrade. The Windows WASAPI path is untouched.

**TCC reality (the hard-won part).** The tap is gated by `kTCCServiceAudioCapture`, SEPARATE from
Screen Recording, with NO Settings toggle — it must be requested via the private TCC SPI
(`TCCAccessRequest`, dlopen of TCC.framework; see `Permission.swift`, mirrors insidegui/AudioCap).
Grant once: `meetflow-capture --request-permission`. TCC keys on the RESPONSIBLE process, so the
sidecar MUST be launched via `open` (LaunchServices) — for the grant prompt AND every capture — so
the `.app` is its own responsible process and uses the bundle grant. A directly-spawned child is
judged against the parent (terminal/daemon) → silence.

**Stable signing.** The `.app` is signed with a STABLE self-signed identity (`codesign` Authority =
`MeetFlow Local Signing`, not ad-hoc), so the `kTCCServiceAudioCapture` grant survives reboots and
re-signed rebuilds with the same cert. Only a rebuild that changes the code identity (or loses the
cert) resets it. Helpers: `~/vg` (grant), `~/vt` (verify) symlinks.

**AEC is OFF and must stay off.** Voice-Processing AEC (`aec = "on"`) is implemented but must not be
enabled: VPIO cannot coexist with another app that owns the mic. On a real call (Zoom/Teams/Meet
holding the mic) the sidecar's VPIO grab hijacks the mic (Dani's audio drops) AND ducks the incoming
audio. The proven route is tap-only (`aec = "off"` in `meetflow.local.toml`): mic via Python
sounddevice, tap-only sidecar, software bleed-dedup. Two constraints for anyone reviving AEC: (1)
the resampler must read the AGGREGATE device's ACTUAL input stream format (`deviceStreamFormat`),
not the tap's advertised 48 kHz — VPIO reconfigures the aggregate to 16 kHz, and using 48 kHz makes
"them" 3× too long/slow → 0 segments; (2) `route_auto_detect` must NOT enable VPIO while the mic is
contended (detect an active call / second mic consumer and stay tap-only). Until both hold, AEC
stays off.

## Pipeline notes

- **No dictation contention**: meetings run on a dedicated whisper-cli process, not the resident
  dictation whisper-server. One model file on disk, loaded transiently per meeting.
- **VAD** (Silero) strips silence before the model sees it → kills silence-hallucinations
  ("Subtitles by Amara.org", "TV Gelderland"); `filters.py` catches the residue.
- **Sonnet extraction** runs in parallel with opus encoding. The parser salvages loose JSON
  field-by-field; a per-call language directive keeps a Dutch call's summary Dutch.
- **INDEX.md** is rebuilt at the end of every run (and via `meetflow index`) — pure derived data.
- **Calendar enrichment** (`integrations/calendar.py`, opt-in `[calendar]`): matches a recording to
  its overlapping calendar event via the local `gws` CLI (read-only) → real title, "them" name
  (attendees), client slug. Degrades to None on any failure. Runs before transcription.
- **Whisper proper-noun vocabulary** (`engine.build_prompt`/`set_meeting_vocab`, `[whisper].glossary`):
  appends known names/companies (glossary + calendar attendees + CRM contact) to the whisper
  `--prompt` so "Oer Sterk"/"Burg"/"HoutCalc" transcribe correctly. Cleared per run.
- **Junk/test auto-quarantine** (`storage/quarantine.py`, `[hygiene]`): short/near-empty/"test"
  recordings are tagged and moved to `meetings/_quarantine/` (reversible, NEVER deleted), excluded
  from INDEX.md with a footer count.
- House style: em/en dashes normalized to plain hyphens in generated notes + calendar titles.

## Troubleshooting (macOS)

- **Ctrl+Alt+M does nothing** → is the daemon running? `launchctl print
  gui/$(id -u)/com.ververflow.meetflow | grep state`. Tail `~/Library/Logs/meetflow.log` for the
  heartbeat. Reload Hammerspoon: `hs -c "hs.reload()"`. Restart:
  `launchctl kickstart -k gui/$(id -u)/com.ververflow.meetflow`.
- **Records silence / "Geen spraak"** → grant **Microphone** to the daemon's venv python (first
  record triggers the prompt; System Settings → Privacy & Security → Microphone).
- **whisper-cli / ffmpeg / claude not found under launchd** → the plist `EnvironmentVariables.PATH`
  must include `/opt/homebrew/bin` + `~/.local/bin`.
- **Two daemons / double recordings** → cannot happen: the portalocker pidfile rejects a second
  instance.

## Known limits

- **Echo in the raw audio without headphones (expected, not a bug):** on speakers the mic picks up
  system audio acoustically, so a played clip lands in BOTH channels. The TRANSCRIPT is clean
  (cross-channel dedup strips the bleed before extraction), so notes/summaries are unaffected.
  Headphones remove it from the audio too.
- Cross-channel dedup is good but not perfect: with heavy speaker bleed an occasional
  duplicate/mis-attributed line can survive. Quiet tap audio is intentionally kept-both.
- Transcription is NL + EN by default; add codes to `[whisper].languages`.

## Not built (V2 ideas)

- Pre-meeting briefs (CRM + previous-meeting context fed to Claude before the call)
- Cross-meeting analytics ("80% of clients mention SEO")
- Meeting → proposal pipeline (Jinja2 templates fed by extraction output)
- Bidirectional commitment tracking across meetings
- Semantic search (vector embeddings on top of FTS5)
