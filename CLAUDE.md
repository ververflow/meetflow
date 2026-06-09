# MeetFlow — context for Claude / contributors

See [README.md](README.md) for user-facing docs. This file captures non-obvious
design context for anyone modifying the code (human or AI).

## What it is

Personal meeting intelligence tool. Records mic + system audio, transcribes
with Whisper, extracts a structured summary via the Claude CLI, stores
everything locally with full-text search.

## Status

V1.1 worked on Windows. macOS port (Apple M5 Pro) is **LIVE and in daily use**: Phases 1–4a
shipped and proven on-box (2026-06-08). Records both channels (mic "me" + CoreAudio tap "them"),
transcribes, extracts, stores, searchable. Survives reboots. Only Phase 4b hardening remains —
see **Remaining work** at the bottom. Day-to-day path: Ctrl+Alt+M → daemon → pipeline → archive.

## macOS port (Phases 1–4a DONE, 2026-06-08)

This tree lives at `~/code/tools/meetflow` (per Dani's structure rule: self-built tools
share the `~/code/tools` repo). Confidential meeting data lives OUTSIDE the repo at
`~/Library/Application Support/MeetFlow` (gitignored, Time-Machine only, never synced).

Approved plan (4 phases): `~/.claude/plans/plan-fase-4-warm-rocket.md`. Day-to-day, Dani's
only action is **Ctrl+Alt+M** (start/stop); everything lands in one fixed dir; NO client
tagging / CRM / Notion (he matches client→recording himself later). Extraction = **Sonnet**.

**Bootstrap (done earlier 2026-06-08):** ported off faster-whisper (no Metal in CTranslate2);
removed the 3 Windows `creationflags` crashes + `os.startfile`; `PyAudioWPatch` gated to win32;
`notify.py` → osascript; robust field-by-field extraction parser + per-call language directive.

**Phase 1 — DONE and proven on the real 16-min Burg call:**
- `transcribe/engine.py` is now a pluggable backend. Default `cli`: a DEDICATED `whisper-cli`
  per channel with Silero VAD (`ggml-silero-v6.2.0.bin`), large-v3, Metal. Own process → does
  NOT block the resident dictation whisper-server; VAD kills silence-hallucinations; failure
  isolated per channel. `server` backend (HTTP /inference) kept as fallback. `CliBackend`
  serializes via an internal `threading.Lock` so `diarize.py`/`filters.py` stay unchanged.
  whisper-cli `-ojf` has no `no_speech_prob` (VAD covers it) — `avg_logprob` is computed from
  per-token `p`. Result: 332 segs in 89s (faster than server's 113s), no hallucinations.
- `extraction.claude_model = "sonnet"` (visibly richer summaries than haiku, ~$0.16/meeting).
- `filters.py`: `is_low_confidence` → OR-form; added `amara.org`/`tv gelderland` patterns.
- `storage/audio.py`: dropped `-ac 1` so opus archives keep source channels (stereo me/them).
- New `[whisper]` config keys (backend/cli_path/model_path/vad_*) in config.py + local.toml.

**Phase 2 — DONE:** data consistency. Folder naming `YYYY-MM-DD_HHMM` (recorder.py; the
`_run_pipeline` date parse now accepts HHMM and legacy HHMMSS). `meetflow index` rebuilds
`<data>/meetings/INDEX.md` (reverse-chron table: date · title · duration · open-actions · folder),
also auto-rebuilt at the end of every pipeline run. `storage/files.py:generate_index` +
`database.py:list_meetings`.

**Phase 3 — DONE (software; live):** trigger + reliability. Trigger lives in a Hammerspoon
`meetflow.lua` (Ctrl+Alt+M toggle + 🎬/🔴/⏳/⚫ menubar, beeps, consent reminder) in
`~/macbook/hammerspoon` — it only writes a one-word command to
`<data>/control/command` and reads `<data>/control/status.json`. New `meetflow daemon`
(`meetflow/daemon.py`) watches that file, runs record→pipeline, writes status.json, holds a
portalocker pidfile (single instance) and logs a heartbeat every 30s. `meetflow start|stop|toggle`
write the same control file (terminal/Raycast can trigger too). LaunchAgent
`~/macbook/launchd/Library/LaunchAgents/com.ververflow.meetflow.plist` mirrors the whisper-server
plist; loaded + running. `capture/loopback.py` degrades to an empty "them" channel on macOS until
Phase 4, so the daemon records mic-only now (sounddevice mic → ch0; ch1 silent). The old
`listen`/`hotkey.py`/`tray.py` (pynput/pystray) stay in the repo but are OUT of the daily path.
ONE manual step remains: the first Ctrl+Alt+M triggers the macOS **Microphone** prompt for the
daemon's venv python — grant it once; after that recording works (and survives reboots).

**Phase 4 split into 4a (tap + diarize fix) and 4b (AEC + route-detect + signed .app).**
Full plan: `~/.claude/plans/plan-fase-4-warm-rocket.md`.

**Phase 4a — DONE and PROVEN on-box (2026-06-08): real system audio captured.**
- `swift-sidecar/` — a compiled CoreAudio process-tap app (`meetflow-capture`, bundled as
  `MeetFlowCapture.app`). Global tap (`CATapDescription(stereoGlobalTapButExcludeProcesses: [])`,
  `muteBehavior=.unmuted` so the call is not silenced) → private aggregate device anchored to the
  default-output clock → realtime IOProc downmixes to mono into a lock-free SPSC ring buffer → a
  drain thread streams a 16 kHz mono Float32 WAV (linear resampler matching `np.interp`). SIGTERM
  does ordered teardown (stop → destroy IOProc → destroy aggregate → destroy tap → flush). Builds
  via `swift-sidecar/build.sh` → `swift build -c release` + assembles & codesigns the `.app`
  (system frameworks only, no Xcode).
- **TCC reality (the hard-won part):** the tap is gated by `kTCCServiceAudioCapture`, which is
  SEPARATE from Screen Recording and has NO Settings toggle — it must be requested via the
  private TCC SPI (`TCCAccessRequest`, dlopen of TCC.framework; see `Permission.swift`, mirrors
  insidegui/AudioCap). Run once: `meetflow-capture --request-permission`. Critically, TCC keys on
  the RESPONSIBLE process, so the sidecar MUST be launched via `open` (LaunchServices) — both for
  the grant prompt AND every capture — so the `.app` is its own responsible process and uses the
  bundle grant. A directly-spawned child is judged against the parent (terminal/daemon) → silence.
- `capture/loopback.py` — `darwin` branch launches `MeetFlowCapture.app` per meeting via
  `open -n … --args --out … --sample-rate 16000`, polls `capture-status.json` for the sidecar pid,
  and stops it with SIGTERM to that pid; reads back `them.wav`. EVERY failure path (missing .app,
  open error, no pid, timeout, missing/empty WAV) returns an empty array → mic-only degrade.
  Windows WASAPI path untouched. `recorder.py` one-line change passes `config.capture` +
  `data_dir`. New `[capture]` config section; `local.toml` `sidecar_path` points at the `.app`.
- `diarize._deduplicate_cross_channel` — rewritten to tap-anchored asymmetric attribution: the
  tap is ground truth for "them"; a "them" segment is never dropped for a bleed-through "me"
  duplicate (kept-both when the tap is silent). Fixes the Windows me/them failure. Unit-tested.
- **Proven on real recordings (2026-06-08):** tap created, ASBD = 48 kHz/2 ch/float, real audio
  captured end-to-end through the production `LoopbackStream` path. Verified meeting separated
  "me" (mic, 3 segs) from "them" (tap: a YouTube clip) with correct attribution; cross-channel
  dedup removed the speaker-bleed duplicate. 24 tests green.
- **Stable signing — DONE (part of 4b, landed early):** the `.app` is signed with a STABLE
  self-signed identity (`codesign` Authority = `MeetFlow Local Signing`, NOT ad-hoc). The
  `kTCCServiceAudioCapture` grant therefore SURVIVES reboots and re-signed rebuilds with the same
  cert. Only a rebuild that changes the code identity (or loses the cert) resets it. Reboot-safety
  verified on-box: daemon (launchd RunAtLoad+KeepAlive, symlinked into ~/Library/LaunchAgents),
  hotkey (Hammerspoon registered login item), and the tap grant all persist across shutdown.
  Helpers: `~/vg` (grant), `~/vt` (verify) symlinks; `swift-sidecar/build.sh`.

**Phase 4b — PARTIAL. Stable cert done; AEC is the open item (and currently BROKEN).**
- ✅ Stable self-signed cert (above) — grant survives reboots/rebuilds.
- ❌ **Voice-Processing AEC (`aec = "on"`) is implemented but BROKEN — do not enable.** When on,
  the sidecar captures both mic ("me") AND tap ("them") in one process (`loopback.py` `capture_mic`,
  `--mic-out`/`--aec`), opt-in via `recorder.py` (`config.capture.aec == "on"`). Tested 2026-06-08
  (meeting 1806): the tap "them" channel comes out at 48 kHz but is read as 16 kHz → 3× too long
  & 3× too slow (124s for a 41s recording) → Whisper finds 0 segments → empty meeting (only a
  `recording.wav`, no json/md). The mic grant itself worked (me = 41.2s captured). **Root cause =
  sample-rate handling in the sidecar's dual mic+tap path; fix before re-enabling.** Until fixed,
  keep `aec = "auto"` (which currently means: mic via Python sounddevice, tap-only sidecar, software
  bleed-dedup — the proven path).
- ❌ Output-route auto-detect: `config.capture.route_auto_detect` is read but UNUSED in the active
  path (no consumer wires it to AEC yet).
- ❌ `meetflow doctor` preflight (sidecar present? grant held? models on disk? paths valid?).

## Architecture (one level deeper than the README) — current macOS reality

```
Ctrl+Alt+M (Hammerspoon meetflow.lua, ~/macbook)
  └─ writes a one-word command to <data>/control/command
       └─ meetflow daemon (launchd com.ververflow.meetflow) watches it
            ├─ capture/  recorder.py → mic.py (sounddevice "me", ch0)
            │            + loopback.py (system "them", ch1 — CoreAudio tap, live since Phase 4a)
            ├─ transcribe/ engine.py  — PLUGGABLE backend:
            │              CliBackend (whisper-cli + Silero VAD, default) | ServerBackend (HTTP)
            │              diarize.py (channel-based + cross-channel dedup) · filters.py
            ├─ extract/  llm.py (Claude CLI sonnet + robust JSON-salvage) · prompts · schema
            └─ storage/  database.py (SQLite+FTS5) · files.py (JSON+MD+INDEX.md) · audio.py (opus)
       └─ daemon writes <data>/control/status.json (state/heartbeat) — Hammerspoon reads it for the glyph
notify.py — macOS osascript notifications.  config.py — TOML loader (local.toml overrides toml).
```

Key design notes for anyone modifying this:
- **Engine backend abstraction** (`transcribe/engine.py`): `transcribe_audio(audio, config, language)
  -> list[Segment]` hides whether whisper-cli or the HTTP server was used. `CliBackend` serializes
  its subprocesses with a `threading.Lock`, so `diarize.py`/`filters.py` need no backend knowledge.
  whisper-cli `-ojf` has no `no_speech_prob`; `avg_logprob` is computed from per-token `p`, and VAD
  is the primary silence filter.
- **Trigger ≠ work**: Hammerspoon owns only the hotkey + menubar (it already has Accessibility);
  the daemon owns capture + pipeline and needs only Microphone (+ Screen Recording in Phase 4).
  IPC is a plain control file + status.json (debuggable, restart-safe). This split is the fix for
  "sometimes doesn't start" (the old pynput listener got zero events without TCC while looking alive).
- **Single instance + heartbeat**: `daemon.py` holds a `portalocker` pidfile and logs a heartbeat
  every 30s + writes it into status.json, so liveness is checkable from `tail` and the menubar.

## Pipeline notes

- **No dictation contention**: meetings run on a dedicated whisper-cli process, not the resident
  dictation whisper-server. One model file on disk, loaded transiently per meeting.
- **VAD** (Silero) strips silence before the model sees it → kills the silence-hallucinations
  ("Subtitles by Amara.org", "TV Gelderland"); `filters.py` catches the residue.
- **Sonnet extraction** runs in parallel with opus encoding. The parser salvages loose JSON
  field-by-field; a per-call language directive keeps a Dutch call's summary Dutch.
- **INDEX.md** is rebuilt at the end of every run (and via `meetflow index`) — pure derived data.
- **Timing**: every stage logs a `[time]` label.

## Troubleshooting (macOS)

- **Ctrl+Alt+M does nothing** → is the daemon running? `launchctl print gui/$(id -u)/com.ververflow.meetflow | grep state`. Tail `~/Library/Logs/meetflow.log` for the heartbeat. Reload Hammerspoon: `hs -c "hs.reload()"`. Restart the daemon from the menubar or `launchctl kickstart -k gui/$(id -u)/com.ververflow.meetflow`.
- **Records silence / "Geen spraak"** → grant **Microphone** to the daemon's venv python (first record triggers the prompt; check System Settings → Privacy & Security → Microphone).
- **whisper-cli / ffmpeg / claude not found under launchd** → the plist `EnvironmentVariables.PATH` must include `/opt/homebrew/bin` + `~/.local/bin` (it does).
- **Two daemons / double recordings** → can't happen: the portalocker pidfile rejects a second instance (it logs and exits).

## Known limits

- **Echo in the raw audio without headphones (expected, not a bug):** on speakers the mic picks
  up the system audio acoustically, so a played clip is in BOTH channels — once clean from the tap,
  once as a delayed/quieter copy via the mic. Listening back, that overlap sounds like an echo. The
  TRANSCRIPT is clean (cross-channel dedup strips the bleed before extraction), so notes/summaries
  are unaffected. Headphones remove the echo from the audio too; the AEC path would as well once
  fixed (see Remaining work).
- Cross-channel dedup is good but not perfect: with heavy speaker bleed an occasional duplicate/
  mis-attributed line can survive in the transcript. Quiet tap audio is intentionally kept-both.
- `client_slug` data hygiene: an untagged meeting can end up with a slug derived from the time
  (e.g. the Burg call shows `client_slug = "110000"`). Cosmetic; revisit with `meetflow tag`.
- Transcription is NL + EN by default; add codes to `[whisper].languages`.

## Remaining work (deferred — pick up next session)

1. **Fix the AEC sample-rate bug** so `aec = "on"` works → clean separation WITHOUT headphones
   (Dani records on speakers). The tap "them" channel is written 48 kHz but read as 16 kHz in the
   dual mic+tap path → 3× duration, 0 transcript (see Phase 4b above, meeting 1806). Highest-value
   open item: it's the difference between "usable with minor echo" and "perfectly clean on speakers".
2. **Output-route auto-detect** (`route_auto_detect`): wire speakers-vs-headphones detection to
   drive AEC automatically. Code reads the flag but nothing consumes it.
3. **`meetflow doctor`** preflight: sidecar `.app` present + grant held? models on disk? cli paths
   valid? daemon loaded? One command to confirm green before a real call.
4. **Clean up the legacy daily-path code** now unused on macOS: `listen`/`hotkey.py`/`tray.py`
   (pynput/pystray) and the Windows `uninstall` wording. Eliminate-before-automate.
5. **`client_slug` hygiene** (see Known limits) — stop deriving a slug from the timestamp.

## V2 roadmap (not built)

- Pre-meeting briefs (CRM + previous meeting context fed to Claude before the call)
- Cross-meeting analytics (e.g. "80% of clients mention SEO")
- Meeting → proposal pipeline (Jinja2 templates fed by extraction output)
- Bidirectional commitment tracking across meetings
- Semantic search (vector embeddings on top of FTS5)
