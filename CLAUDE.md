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
transcribes, extracts, stores, searchable. Survives reboots. Day-to-day path: Ctrl+Alt+M → daemon → pipeline → archive. **For the current
state read the 2026-07-06 section directly below**; the phase history further down is the earlier
record and AEC/Phase-4b is the only genuinely open item.

## 2026-07-06 — lanes, organization, resilience (big session; READ THIS FIRST)

**Three capture lanes now share the engine + store:**
- **Meetings** (2+ people): Ctrl+Alt+M → daemon → stereo mic/tap → per-channel whisper-cli + VAD →
  diarize → Sonnet extraction → store. `kind='meeting'` (Phases 1-4a, below).
- **Journal / brainstorm** (solo, lane C, NEW): **Hyper+J** → daemon `journal` verb → recorder
  `mic_only` (no tap, no diarization, every segment = "me") → whisper-cli (VAD + auto nl/en +
  `max_context=0`) → a JOURNAL distillation (themes/insights/decisions/open-questions/todos/
  notes_to_claude) via the journal system prompt → `kind='journal'`, stored in `<data>/journal/` +
  `JOURNAL.md`, viewed at `~/journal`. New module `meetflow/journal.py` is a PARALLEL pipeline that
  never branches into the meeting hot path. This is the spoken journal the brain harvests.
- **Dictation** (text-to-cursor) is a SEPARATE tool in `~/macbook` (dictation.lua + the resident
  whisper-server on :8771), NOT this repo — but it now shares the vocab SSOT and runs `-l auto` + VAD.

**Output organization — orthogonal axes, not a folder tree.** `client_slug` used to conflate three
things (venture, client, and even "journal" as a value). Split into: `kind` (meeting|journal) +
`venture` (houtcalc | ververflow | creator-partnerships[retired] | "") + `type` (discovery |
working-session | partner-sync | product-feedback | user-interview | reflection | brainstorm) +
`client_slug` = just the counterparty. New meetings auto-set `venture` from the counterparty
(`config.venture_for`, HoutCalc-slugs → houtcalc, else agency); journals are kind=journal (not a
venture), default type=reflection. INDEX.md is now GROUPED PER VENTURE (a derived view over flat
storage). DB gained `venture`/`type` columns (migrated on open, indexed). Re-tag any recording:
`meetflow classify <id> --venture … --type … --counterparty …`.

**Resilience (all live).**
- The LLM step is NON-FATAL in BOTH lanes: on failure (e.g. the Claude usage limit) the transcript
  is still saved + tagged `distillatie-mislukt`; recover with `meetflow redistill <id>` (re-distils
  the SAVED transcript — no re-record, no re-transcribe). This fixed a real data-loss bug (a journal
  lost its transcript when a Claude-limit crash hit AFTER transcription but BEFORE save; the audio
  survived as opus, so it was recoverable via `process --kind journal`).
- No-speech no longer strands orphan WAVs: meetings archive+quarantine, journals discard the silent clip.
- The daemon runs the pipeline in a BACKGROUND THREAD, so the menubar stays live during processing and
  a toggle pressed mid-processing no longer fires a surprise recording (the stale command is drained).
- Anti-loop: `filters.collapse_repeated_segments` (deterministic backstop) + journal `max_context=0`
  (A/B-proven; the old journal looped one sentence 17x because it had gone through the wrong engine).
- FTS gained AFTER DELETE/UPDATE triggers + a one-time rebuild, so search no longer desyncs on re-index.

**New CLI:** `journal` (toggle a solo session), `redistill <id>`, `classify <id> …`,
`process --kind journal`. **New config:** `[journal]` (dirname, max_context), `venture_for`, whisper
`fixups`/`fixups_brand`, `apply_vocab_ssot` (merges `~/.config/whisper/vocab.json` + `vocab.local.json`
into the glossary + fixups at CLI startup — NOT in load_config, so tests keep an empty glossary).
Fixups correct the transcript-of-record ("fair flow" → VerverFlow), not just the summary. Config
defaults aligned to the template (sonnet, 48k). Recorder `__init__` dead-code bug fixed. 67 tests green.

## macOS port (Phases 1–4a DONE, 2026-06-08)

This tree lives at `~/code/tools/meetflow` (per Dani's structure rule: self-built tools
share the `~/code/tools` repo). Confidential meeting data lives OUTSIDE the repo at
`~/Library/Application Support/MeetFlow` (gitignored, Time-Machine only, never synced).

Day-to-day, Dani's
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
- `client_slug` data hygiene: FIXED 2026-06-20. The timestamp-slug bug is gone (the `process`
  command no longer derives a slug from the folder time); untagged meetings are `unknown` unless
  calendar enrichment supplies a slug. The DB column is the source of truth; correct with
  `meetflow tag`.
- Transcription is NL + EN by default; add codes to `[whisper].languages`.

## Accuracy + enrichment (2026-06-20)

Reflecting on the first 8 real meetings drove an accuracy/enrichment pass (all Python, tests green):
- **Google Calendar enrichment** (`integrations/calendar.py`, opt-in `[calendar]`): matches a
  recording to its overlapping calendar event via the local `gws` CLI (read-only) and fills the
  real meeting title, the "them" name (from attendees), and a client slug. Degrades to None on any
  failure. Wired into `_run_pipeline` before transcription (title/them/slug + extraction context).
- **Whisper proper-noun vocabulary** (`engine.build_prompt`/`set_meeting_vocab`, `[whisper].glossary`):
  appends known names/companies (glossary + calendar attendees + CRM contact) to the whisper
  `--prompt` so "Oer Sterk"/"Burg"/"HoutCalc" transcribe correctly. Cleared per run.
- **client_slug hygiene**: the timestamp-slug bug is gone; the DB `client_slug` column is the single
  source of truth. `delete`/`export` resolve folders via DB `json_path` (not a name suffix); `tag`
  no longer rewrites the id.
- **Junk/test auto-quarantine** (`storage/quarantine.py`, `[hygiene]`): short/near-empty/"test"
  recordings are tagged and moved to `meetings/_quarantine/` (reversible, NEVER deleted) and excluded
  from INDEX.md (with a footer count).
- **`meetflow doctor`** (preflight) and **`meetflow backfill`** (re-extract titles + repair old
  Windows mojibake + reconcile slug for old recordings) commands.
- House style: em/en dashes normalized to plain hyphens in generated notes + calendar titles.

## Remaining work (deferred — pick up next session)

> **AEC route-auto-detect REVERTED to `off`, 2026-06-22.** Items 1 and 2 below called `aec =
> "auto"` on built-in speakers DONE + LIVE. The one un-exercised path (a REAL call with actual
> voice) was hit in production and BROKE the call: with the call app (Zoom/Teams/Meet) already
> holding the mic, the sidecar's VPIO grab hijacked Dani's mic (his audio dropped) AND ducked the
> incoming audio (the other party went much quieter). VPIO cannot coexist with another app that
> owns the mic, so AEC must not engage during a live call. Immediate fix applied: `aec = "off"` in
> `meetflow.local.toml` (the proven tap-only route) + daemon restarted. Proper fix (DEFERRED):
> `route_auto_detect` must NOT enable VPIO while the mic is contended (detect an active call or a
> second mic consumer and stay tap-only). Until that lands, keep `aec = "off"`; do not re-enable.

1. **AEC sample-rate fix — DONE + LIVE 2026-06-20.** Root cause: the resampler used the tap's
   advertised 48 kHz, but the IOProc reads from the AGGREGATE device, which VPIO (dual mic+tap)
   reconfigures to 16 kHz → the 3× bug. Fix in `ProcessTap.start()`: read the aggregate device's
   ACTUAL input stream format and drive the ring/resampler/downmix from that (`deviceStreamFormat`).
   Built + signed via `build.sh` with the stable "MeetFlow Local Signing" identity (grant survived).
   Proven on-box with `verify-tap.sh`: `them.wav` = 7.58s for ~7.6s wall-clock (1:1, NOT 3×), me/them
   aligned. Both TCC grants confirmed present (`kTCCServiceAudioCapture` + `kTCCServiceMicrophone` =
   2). Only un-exercised bit: a clean "me" on a REAL call with actual voice (AEC cancels speaker
   audio, so it can't be tested without talking).
2. **Output-route auto-detect — DONE + LIVE 2026-06-20.** `recorder._resolve_aec()` probes the
   sidecar (`meetflow-capture --route-json`, added to `main.swift`) and turns AEC on only for
   built-in speakers. Self-gating (older sidecar without `--route-json` → stays tap-only). With the
   rebuilt sidecar the daemon now logs `route_auto_detect: builtInSpeakers -> AEC on`, so `aec =
   "auto"` uses the dual VPIO path on speakers. Restart the daemon after a route change (the probe
   runs in `Recorder.__init__` at daemon start).
3. **Clean up the legacy daily-path code** now unused on macOS: `listen`/`hotkey.py`/`tray.py`
   (pynput/pystray) and the Windows `uninstall` wording. Eliminate-before-automate.

Done 2026-06-20: AEC fix (live), route auto-detect (live), `meetflow doctor`, `client_slug` hygiene,
calendar enrichment, whisper glossary, junk quarantine, backfill.

## V2 roadmap (not built)

- Pre-meeting briefs (CRM + previous meeting context fed to Claude before the call)
- Cross-meeting analytics (e.g. "80% of clients mention SEO")
- Meeting → proposal pipeline (Jinja2 templates fed by extraction output)
- Bidirectional commitment tracking across meetings
- Semantic search (vector embeddings on top of FTS5)
