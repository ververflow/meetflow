"""MeetFlow CLI — record, transcribe, extract, search."""
from __future__ import annotations

import contextlib
import logging
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import click

from meetflow.config import load_config

log = logging.getLogger("meetflow")


def _ensure_model_loaded(config) -> None:
    """Load Whisper model if not already loaded. Safe to call from any thread."""
    from meetflow.transcribe.engine import get_model, load_model

    try:
        get_model()
    except RuntimeError:
        log.info("Pre-loading Whisper model in background...")
        load_model(config.whisper)
        log.info("Whisper model ready")


def _echo(msg: str, **kwargs) -> None:
    """click.echo that silently ignores errors when stdout is unavailable (pythonw)."""
    try:
        click.echo(msg, **kwargs)
    except OSError:
        pass


@contextlib.contextmanager
def _timed(label: str):
    """Log elapsed time for a pipeline step."""
    t0 = time.perf_counter()
    yield
    elapsed = time.perf_counter() - t0
    log.info("[time] %s: %.2fs", label, elapsed)


from meetflow.text import sanitize_slug as _sanitize_slug  # noqa: E402 (re-export for back-compat)


def _rebuild_index(config) -> Path:
    """Regenerate INDEX.md (the meetings overview) from the database."""
    from meetflow.storage.database import MeetingDB
    from meetflow.storage.files import generate_index

    db = MeetingDB(config.data_dir / "meetflow.db")
    meetings = db.list_meetings()
    db.close()
    return generate_index(meetings, config.data_dir / "meetings", config.hygiene.quarantine_dirname)


# Global recorder state (module-level for signal handling)
_recorder = None
_config = None


def _setup_logging() -> None:
    from logging.handlers import RotatingFileHandler

    log_file = Path(__file__).parent.parent / "meetflow.log"
    handlers = [
        RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
    ]
    # Only add stream handler if stdout is writable (not pythonw)
    if sys.stdout is not None and hasattr(sys.stdout, "fileno"):
        try:
            sys.stdout.fileno()
            handlers.append(logging.StreamHandler(sys.stdout))
        except (OSError, ValueError):
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


@click.group()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None, help="Path to meetflow.toml")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """MeetFlow — Personal meeting intelligence."""
    _setup_logging()
    ctx.ensure_object(dict)
    path = Path(config_path) if config_path else None
    from meetflow.config import apply_vocab_ssot

    ctx.obj["config"] = load_config(path)
    apply_vocab_ssot(ctx.obj["config"])  # merge the shared vocab/fixups SSOT into whisper config


@cli.command()
@click.argument("client_slug", required=False)
@click.pass_context
def record(ctx: click.Context, client_slug: str | None) -> None:
    """Start recording a meeting. Press Ctrl+C to stop."""
    global _recorder, _config
    config = ctx.obj["config"]
    _config = config

    from meetflow.capture.recorder import Recorder

    _recorder = Recorder(config)

    def handle_stop(signum, frame):
        click.echo("\nStopping recording...")
        _stop_and_process(_recorder, config, client_slug)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_stop)

    _recorder.start()
    click.echo(f"Recording... (client={client_slug or 'unknown'})")
    click.echo("Press Ctrl+C to stop and process.")

    # Keep alive
    try:
        while True:
            elapsed = _recorder.elapsed_seconds
            mins, secs = divmod(int(elapsed), 60)
            click.echo(f"\r  {mins:02d}:{secs:02d} recording", nl=False)
            time.sleep(1)
    except KeyboardInterrupt:
        pass


@cli.command()
@click.argument("client_slug", required=False)
@click.pass_context
def listen(ctx: click.Context, client_slug: str | None) -> None:
    """Daemon mode — double-tap Right Ctrl to start/stop recording."""
    config = ctx.obj["config"]

    from meetflow.capture.hotkey import HotkeyListener, beep_done, beep_error, beep_ready, beep_start, beep_stop
    from meetflow.capture.recorder import Recorder
    from meetflow.capture.tray import TrayIcon
    from meetflow.notify import notify, set_tray
    import threading

    recorder = Recorder(config)
    tray = TrayIcon()
    tray.start()
    set_tray(tray)
    is_recording = False
    processing = False
    toggle_lock = threading.Lock()

    # Lazy model loading — don't claim GPU at startup so whisper-hotkey keeps working
    log.info("MeetFlow ready (Whisper model loads on first recording stop)")
    beep_ready()

    def on_toggle() -> None:
        nonlocal is_recording, processing
        if not toggle_lock.acquire(blocking=False):
            return
        try:
            _on_toggle_inner()
        finally:
            toggle_lock.release()

    def _on_toggle_inner() -> None:
        nonlocal is_recording, processing
        if processing:
            log.info("Still processing previous recording, please wait")
            return
        if not is_recording:
            beep_start()
            recorder.start()
            tray.set_recording(client_slug or "")
            is_recording = True
            log.info("Recording started (double-tap Right Ctrl)")
            # Pre-load Whisper model in background while recording runs
            threading.Thread(target=_ensure_model_loaded, args=(config,), daemon=True).start()
        else:
            is_recording = False
            processing = True
            tray.set_processing()
            beep_stop()
            log.info("Recording stopped, processing...")
            wav_path = recorder.stop()
            if wav_path:
                try:
                    meeting = _run_pipeline(wav_path, config, client_slug)
                    beep_done()
                    meeting_dir = wav_path.parent
                    if meeting:
                        n_seg = len(meeting.transcript)
                        summary = meeting.extraction.summary[:150]
                        n_actions = len(meeting.extraction.action_items.i_owe_them) + len(meeting.extraction.action_items.they_owe_me)
                        notify(
                            f"Meeting opgeslagen ({meeting.duration_seconds // 60}m {meeting.duration_seconds % 60}s, {n_seg} segmenten)",
                            f"{summary}" + (f"\n{n_actions} actiepunten" if n_actions else ""),
                        )
                    else:
                        notify("Meeting opgeslagen", "Geen spraak gedetecteerd")
                    # Open the meeting folder in the file manager (cross-platform)
                    import subprocess
                    _opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
                    subprocess.run([_opener, str(meeting_dir)])
                except Exception as e:
                    log.exception("Pipeline failed")
                    beep_error()
                    notify("MeetFlow error", str(e)[:200])
            tray.set_idle()
            processing = False
            log.info("Ready for next recording")

    hotkey = HotkeyListener(on_toggle=on_toggle)
    hotkey.start()

    slug_display = client_slug or "no client"
    _echo(f"MeetFlow listening ({slug_display})")
    _echo("Double-tap Right Ctrl to start/stop recording. Ctrl+C to quit.")

    try:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        if is_recording:
            _echo("\nStopping active recording...")
            wav_path = recorder.stop()
            if wav_path:
                _run_pipeline(wav_path, config, client_slug)
        hotkey.stop()
        tray.stop()
        _echo("MeetFlow stopped.")


def _stop_and_process(recorder, config, client_slug: str | None) -> None:
    """Stop recording and run the full processing pipeline."""
    wav_path = recorder.stop()
    if wav_path is None:
        _echo("No audio captured.")
        return

    _echo(f"Recording saved: {wav_path}")
    _run_pipeline(wav_path, config, client_slug)


def _run_pipeline(wav_path: Path, config, client_slug: str | None):
    """Run the full processing pipeline on a WAV file. Returns the Meeting object or None."""
    from meetflow.extract.schema import Extraction, Meeting, Participants, Recording, TranscriptSegment
    from meetflow.integrations.crm import update_profile_with_meeting
    from meetflow.storage.audio import wav_to_opus
    from meetflow.storage.database import MeetingDB
    from meetflow.storage.files import save_meeting_json, save_meeting_markdown
    from meetflow.transcribe.diarize import transcribe_stereo
    from meetflow.transcribe.engine import get_model, load_model

    pipeline_start = time.perf_counter()

    # 1. Load model (skips if already loaded)
    with _timed("model_check"):
        try:
            get_model()
        except RuntimeError:
            _echo("Loading Whisper model...")
            load_model(config.whisper)

    # 2. Meeting metadata from the folder name (no transcript dependency), so the calendar
    #    lookup + Whisper vocabulary can run BEFORE transcription.
    slug = _sanitize_slug(client_slug) if client_slug else "unknown"
    dir_name = wav_path.parent.name
    parts = dir_name.split("_")
    date_str = parts[0] if len(parts) >= 1 else datetime.now().strftime("%Y-%m-%d")
    time_str = parts[1] if len(parts) >= 2 else "000000"
    meeting_id = dir_name  # e.g. "2026-04-13_143000"

    import soundfile as sf

    info = sf.info(str(wav_path))
    duration_seconds = int(info.duration)
    if len(time_str) == 6:  # legacy HHMMSS folders
        start_time = f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"
    elif len(time_str) == 4:  # current HHMM folders
        start_time = f"{time_str[:2]}:{time_str[2:4]}:00"
    else:
        start_time = "00:00:00"
    try:
        start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M:%S")
        end_time = (start_dt + timedelta(seconds=duration_seconds)).strftime("%H:%M:%S")
    except ValueError:
        end_time = ""

    # 3. Calendar enrichment (read-only; degrades to None). Upgrades an "unknown" slug.
    from meetflow.integrations.calendar import find_event, get_calendar_context
    from meetflow.integrations.crm import get_client_context, read_profile

    cal_match = find_event(config.calendar, date_str, start_time, duration_seconds)
    if slug == "unknown" and cal_match and cal_match.slug_hint:
        slug = cal_match.slug_hint
        _echo(f"  client from calendar: {slug}")

    # 4. Whisper proper-noun vocabulary (calendar attendees + CRM contact + static glossary).
    profile = read_profile(config.crm, slug) if slug != "unknown" else None
    vocab = list(cal_match.them_names) if cal_match else []
    if profile:
        contact = profile.get("contact", {})
        nm = contact.get("naam") or contact.get("name")
        if nm:
            vocab.append(nm)
    from meetflow.transcribe.engine import set_meeting_vocab

    set_meeting_vocab(vocab)

    # 5. Transcribe with diarization (vocab is now in the whisper prompt).
    _echo("Transcribing...")
    try:
        with _timed("transcribe"):
            diarized = transcribe_stereo(wav_path, config.whisper)
    finally:
        set_meeting_vocab([])  # never leak vocab into the next meeting
    if not diarized:
        # Do NOT return here: fall through so an empty/accidental recording is transcoded, saved,
        # quarantined, and indexed — TRACKED and reversible instead of stranding a raw recording.wav
        # no command can see (the 2026-07-01_0235 orphan). extract_meeting_data skips the LLM at 0 segs.
        _echo("No speech detected — archiving + quarantining (no orphan).")

    # Correct the transcript-of-record (not just the LLM summary): the shared fixups fix
    # "how to call"/"fair flow"-style manglings before the LLM, DB, and meeting.md see them.
    from meetflow.text import apply_fixups

    for s in diarized:
        s.text = apply_fixups(s.text, config.whisper.fixups, config.whisper.fixups_brand)

    _echo(f"  {len(diarized)} segments transcribed")

    # Detect primary language
    languages = [s.language for s in diarized]
    primary_lang = max(set(languages), key=languages.count) if languages else "nl"

    # 6. Extract structured data via Claude Code CLI
    from meetflow.extract.llm import extract_meeting_data

    from concurrent.futures import Future, ThreadPoolExecutor

    crm_context = get_client_context(config.crm, slug) if slug != "unknown" else ""
    cal_context = get_calendar_context(config.calendar, cal_match)
    client_context = "\n".join(p for p in (cal_context, crm_context) if p)
    segments_for_llm = [{"speaker": s.speaker, "start": s.start, "end": s.end, "text": s.text} for s in diarized]

    # Start opus encoding in background (independent of extraction)
    opus_future: Future | None = None
    opus_executor: ThreadPoolExecutor | None = None
    if config.storage.archive_format == "opus":
        opus_executor = ThreadPoolExecutor(max_workers=1)
        opus_future = opus_executor.submit(
            wav_to_opus, wav_path, config.storage.opus_bitrate, not config.storage.keep_wav
        )
        log.info("Opus transcode started in background")

    # 4. Extract structured data via Claude Code CLI (runs parallel with opus). If extraction FAILS
    #    (e.g. the Claude usage limit), do NOT lose the transcript: fall back to an empty extraction,
    #    tag it, and still save + index. Re-run later (no re-transcribe) with `meetflow redistill <id>`.
    with _timed("extract"):
        try:
            extraction = extract_meeting_data(segments_for_llm, config.extraction, client_context, language=primary_lang)
            extraction_ok = True
        except Exception as e:  # noqa: BLE001 — a failed extraction must not lose the transcript
            log.warning("Meeting extraction failed (%s); saving transcript, re-run `meetflow redistill %s`", e, meeting_id)
            extraction = Extraction()
            extraction_ok = False

    # 5. Build Meeting object
    meeting = Meeting(
        id=meeting_id,
        client_slug=slug,
        meeting_title=(cal_match.title if cal_match and cal_match.title else extraction.meeting_title),
        date=date_str,
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration_seconds,
        language=primary_lang,
        participants=Participants(me=config.my_name),
        transcript=[TranscriptSegment(speaker=s.speaker, start=s.start, end=s.end, text=s.text) for s in diarized],
        extraction=extraction,
    )

    # Resolve "them": CRM contact > real calendar attendee > LLM extraction > calendar hint.
    them = ""
    if profile:
        contact = profile.get("contact", {})
        them = contact.get("naam") or contact.get("name") or ""
    if not them and cal_match and cal_match.has_real_attendees and cal_match.them_names:
        them = cal_match.them_names[0]
    if not them and extraction.them_name:
        them = extraction.them_name
    if not them and cal_match and cal_match.them_names:
        them = cal_match.them_names[0]
    meeting.participants.them = them
    if not diarized:
        meeting.tags.append("no-speech")  # explicit marker; is_junk quarantines it below anyway
    if not extraction_ok:
        meeting.tags.append("distillatie-mislukt")  # transcript kept; re-run `meetflow redistill <id>`

    # Collect opus result (blocks if still encoding)
    opus_path = None
    if opus_future is not None:
        with _timed("transcode_wait"):
            opus_path = opus_future.result()
        if opus_executor:
            opus_executor.shutdown(wait=False)
        meeting.recording = Recording(
            opus_path=opus_path.name,
            opus_size_mb=round(opus_path.stat().st_size / (1024 * 1024), 2),
        )

    # 7. Save files
    meeting_dir = wav_path.parent
    with _timed("save_files"):
        json_path = save_meeting_json(meeting, meeting_dir)
        save_meeting_markdown(meeting, meeting_dir)

    # 8. Quarantine test/junk recordings (tag + move under _quarantine/; NEVER delete).
    from meetflow.storage.quarantine import is_junk, quarantine_meeting

    junk, junk_reason = is_junk(meeting, config.hygiene)
    if junk:
        _echo(f"  quarantined (test/junk): {junk_reason}")
        if config.hygiene.quarantine_tag not in meeting.tags:
            meeting.tags.append(config.hygiene.quarantine_tag)
        save_meeting_json(meeting, meeting_dir)  # rewrite with the tag before the move
        try:
            meeting_dir = quarantine_meeting(meeting_dir, config.data_dir, config.hygiene)
            json_path = meeting_dir / "meeting.json"
            if opus_path:
                opus_path = meeting_dir / opus_path.name
        except Exception:
            log.warning("Quarantine move failed", exc_info=True)

    # 9. Index in database (json_path/opus_path already point at the quarantine dir if moved,
    #    so the rebuilt INDEX.md self-excludes it).
    with _timed("db_index"):
        db = MeetingDB(config.data_dir / "meetflow.db")
        db.index_meeting(meeting, str(json_path), str(opus_path) if opus_path else None)
        db.close()

    # Rebuild the meetings overview (INDEX.md)
    try:
        _rebuild_index(config)
    except Exception:
        log.warning("Index rebuild failed", exc_info=True)

    # 10. Update CRM (non-blocking — fire and forget). Skip quarantined/junk meetings.
    if slug != "unknown" and not junk:
        ThreadPoolExecutor(max_workers=1).submit(
            update_profile_with_meeting, config.crm, meeting, config.my_name
        )

    pipeline_elapsed = time.perf_counter() - pipeline_start
    log.info("[time] pipeline_total: %.2fs", pipeline_elapsed)

    # Print summary
    _echo("\n" + "=" * 60)
    _echo(f"Meeting processed: {meeting_id}")
    dur = meeting.duration_seconds
    _echo(f"Duration: {dur // 60}m {dur % 60}s | Language: {primary_lang}")
    _echo(f"Summary: {extraction.summary}")
    if extraction.action_items.i_owe_them:
        _echo("\nI owe them:")
        for a in extraction.action_items.i_owe_them:
            _echo(f"  - {a.what}" + (f" (deadline: {a.deadline})" if a.deadline else ""))
    if extraction.action_items.they_owe_me:
        _echo("\nThey owe me:")
        for a in extraction.action_items.they_owe_me:
            _echo(f"  - {a.what}" + (f" (deadline: {a.deadline})" if a.deadline else ""))
    _echo("=" * 60)
    return meeting


def _run_journal(wav_path: Path, config):
    """The lane-C journal pipeline: injected into the daemon and used by `process --kind journal`."""
    from meetflow.journal import run_journal_pipeline

    return run_journal_pipeline(wav_path, config)


@cli.command()
@click.argument("wav_path", type=click.Path(exists=True))
@click.option("--client", "client_slug", default=None, help="Client slug for CRM linkage")
@click.option("--kind", type=click.Choice(["meeting", "journal"]), default="meeting", help="Which pipeline to run")
@click.pass_context
def process(ctx: click.Context, wav_path: str, client_slug: str | None, kind: str) -> None:
    """Process an existing WAV recording through the pipeline (meeting or journal)."""
    config = ctx.obj["config"]
    path = Path(wav_path)
    if kind == "journal":
        _run_journal(path, config)
        return
    # Do NOT infer a slug from the folder name: folders are "YYYY-MM-DD_HHMM", so the old
    # split("_") took the TIME as the slug (the "110000" bug). Leave it None -> "unknown",
    # which calendar enrichment can still upgrade to a real client slug.
    _run_pipeline(path, config, client_slug)


@cli.command()
@click.argument("meeting_id")
@click.pass_context
def redistill(ctx: click.Context, meeting_id: str) -> None:
    """Re-run the LLM distillation on an ALREADY-transcribed recording — no re-record, no
    re-transcribe. Use when extraction failed (e.g. the Claude usage limit tagged it
    'distillatie-mislukt'): the transcript was kept, so this just re-distils it and rewrites
    meeting.json / .md and the DB row. MEETING_ID is the folder name (e.g. 2026-07-06_1608).
    """
    import json as _json

    config = ctx.obj["config"]
    from meetflow.extract.schema import Meeting
    from meetflow.storage.database import MeetingDB
    from meetflow.storage.files import save_journal_markdown, save_meeting_json, save_meeting_markdown

    json_path = None
    for sub in ("meetings", config.journal.dirname, f"meetings/{config.hygiene.quarantine_dirname}"):
        cand = config.data_dir / sub / meeting_id / "meeting.json"
        if cand.exists():
            json_path = cand
            break
    if json_path is None:
        click.echo(f"meeting.json not found for {meeting_id}")
        return

    meeting = Meeting(**_json.loads(json_path.read_text(encoding="utf-8")))
    if not meeting.transcript:
        click.echo("No transcript to re-distil.")
        return

    segs = [{"speaker": s.speaker, "start": s.start, "end": s.end, "text": s.text} for s in meeting.transcript]
    if meeting.kind == "journal":
        from meetflow.extract.llm import extract_journal_data

        journal = extract_journal_data(segs, config.extraction, language=meeting.language)
        meeting.journal = journal
        meeting.meeting_title = journal.title or meeting.meeting_title
        meeting.extraction.meeting_title = journal.title
        meeting.extraction.summary = journal.summary
    else:
        from meetflow.extract.llm import extract_meeting_data

        meeting.extraction = extract_meeting_data(segs, config.extraction, language=meeting.language)
        if meeting.extraction.meeting_title:
            meeting.meeting_title = meeting.extraction.meeting_title
    if "distillatie-mislukt" in meeting.tags:
        meeting.tags.remove("distillatie-mislukt")

    save_meeting_json(meeting, json_path.parent)
    (save_journal_markdown if meeting.kind == "journal" else save_meeting_markdown)(meeting, json_path.parent)

    audio_path = None
    if meeting.recording and meeting.recording.opus_path:
        audio_path = str(json_path.parent / meeting.recording.opus_path)
    db = MeetingDB(config.data_dir / "meetflow.db")
    db.index_meeting(meeting, str(json_path), audio_path)
    db.close()

    if meeting.kind == "journal":
        from meetflow.journal import _rebuild_journal_index

        _rebuild_journal_index(config)
    else:
        _rebuild_index(config)
    click.echo(f"Re-distilled {meeting_id} ({meeting.kind}): {meeting.meeting_title}")


@cli.command()
@click.pass_context
def index(ctx: click.Context) -> None:
    """Rebuild INDEX.md — the reverse-chronological overview of all meetings."""
    config = ctx.obj["config"]
    path = _rebuild_index(config)
    click.echo(f"Index rebuilt: {path}")


@cli.command()
@click.pass_context
def daemon(ctx: click.Context) -> None:
    """Run the background daemon (driven by the control file, e.g. Hammerspoon Ctrl+Alt+M)."""
    config = ctx.obj["config"]
    from meetflow.daemon import run_daemon

    run_daemon(config, _run_pipeline, _run_journal)


@cli.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Tell the running daemon to start recording."""
    from meetflow.daemon import write_command

    write_command(ctx.obj["config"], "start")
    click.echo("start")


@cli.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Tell the running daemon to stop recording and process."""
    from meetflow.daemon import write_command

    write_command(ctx.obj["config"], "stop")
    click.echo("stop")


@cli.command()
@click.pass_context
def toggle(ctx: click.Context) -> None:
    """Toggle recording on the running daemon (start if idle, stop if recording)."""
    from meetflow.daemon import write_command

    write_command(ctx.obj["config"], "toggle")
    click.echo("toggle")


@cli.command()
@click.pass_context
def journal(ctx: click.Context) -> None:
    """Toggle a solo journaling / brainstorm session on the running daemon (lane C, Hyper+J)."""
    from meetflow.daemon import write_command

    write_command(ctx.obj["config"], "journal")
    click.echo("journal")


@cli.command()
@click.argument("meeting_dir")
@click.argument("client_slug")
@click.pass_context
def tag(ctx: click.Context, meeting_dir: str, client_slug: str) -> None:
    """Tag a meeting with a client slug and update CRM.

    MEETING_DIR is the meeting folder name (e.g. 2026-04-13_143000).
    """
    import json

    config = ctx.obj["config"]
    meetings_base = config.data_dir / "meetings"

    # Find meeting directory
    meeting_path = meetings_base / meeting_dir
    if not meeting_path.exists():
        # Try partial match
        matches = [d for d in meetings_base.iterdir() if d.is_dir() and meeting_dir in d.name]
        if len(matches) == 1:
            meeting_path = matches[0]
        elif len(matches) > 1:
            click.echo(f"Multiple matches for '{meeting_dir}':")
            for m in matches:
                click.echo(f"  {m.name}")
            return
        else:
            click.echo(f"Meeting not found: {meeting_dir}")
            return

    json_path = meeting_path / "meeting.json"
    if not json_path.exists():
        click.echo(f"No meeting.json in {meeting_path.name}")
        return

    # Update meeting.json
    from meetflow.extract.schema import Meeting
    from meetflow.integrations.crm import read_profile, update_profile_with_meeting
    from meetflow.storage.database import MeetingDB

    data = json.loads(json_path.read_text(encoding="utf-8"))
    data["client_slug"] = client_slug
    # Keep id == folder name. The DB client_slug column is the single source of truth; the
    # slug is never encoded into the id or folder name (so delete/export stay consistent).

    # Get contact name from CRM
    profile = read_profile(config.crm, client_slug)
    if profile:
        contact = profile.get("contact", {})
        name = contact.get("naam") or contact.get("name")
        if name:
            data["participants"]["them"] = name

    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Re-generate markdown
    meeting = Meeting.model_validate(data)
    from meetflow.storage.files import save_meeting_markdown
    save_meeting_markdown(meeting, meeting_path)

    # Update database (id unchanged -> INSERT OR REPLACE updates the row in place)
    db = MeetingDB(config.data_dir / "meetflow.db")
    db.index_meeting(meeting, str(json_path), data.get("recording", {}).get("opus_path"))
    db.close()

    # Update CRM
    if config.crm.enabled:
        update_profile_with_meeting(config.crm, meeting, config.my_name)

    click.echo(f"Tagged {meeting_path.name} -> {client_slug}")
    if profile:
        click.echo(f"  Contact: {data['participants'].get('them', '')}")
    click.echo(f"  CRM updated: {config.crm.enabled and profile is not None}")


@cli.command()
@click.argument("query")
@click.option("--limit", default=10, help="Max results")
@click.pass_context
def search(ctx: click.Context, query: str, limit: int) -> None:
    """Search across all meeting transcripts."""
    config = ctx.obj["config"]
    from meetflow.storage.database import MeetingDB

    db = MeetingDB(config.data_dir / "meetflow.db")
    results = db.search(query, limit)
    db.close()

    if not results:
        click.echo(f"No results for '{query}'")
        return

    click.echo(f"Found {len(results)} results for '{query}':\n")
    for r in results:
        click.echo(f"  [{r['date']}] {r['client_slug']} — {r['speaker']}: {r['snippet']}")
        if r.get("summary"):
            click.echo(f"    Summary: {r['summary'][:80]}")
        click.echo()


@cli.command()
@click.option("--client", "client_slug", default=None, help="Filter by client")
@click.pass_context
def actions(ctx: click.Context, client_slug: str | None) -> None:
    """Show open action items across all meetings."""
    config = ctx.obj["config"]
    from meetflow.storage.database import MeetingDB

    db = MeetingDB(config.data_dir / "meetflow.db")
    items = db.get_open_actions(client_slug)
    db.close()

    if not items:
        click.echo("No open action items.")
        return

    click.echo(f"Open action items ({len(items)}):\n")
    for item in items:
        direction = "-> Ik" if item["direction"] == "i_owe_them" else "<- Zij"
        deadline = f" (deadline: {item['deadline']})" if item.get("deadline") else ""
        click.echo(f"  {direction}: {item['what']}{deadline}")
        click.echo(f"    Client: {item['client_slug']} | Meeting: {item['meeting_date']}")
        click.echo()


@cli.command()
@click.option("--client", "client_slug", default=None, help="Filter by client")
@click.pass_context
def history(ctx: click.Context, client_slug: str | None) -> None:
    """Show meeting history."""
    config = ctx.obj["config"]
    from meetflow.storage.database import MeetingDB

    db = MeetingDB(config.data_dir / "meetflow.db")

    if client_slug:
        meetings = db.get_meetings_by_client(client_slug)
    else:
        cur = db._conn.cursor()
        cur.execute("SELECT id, client_slug, date, duration_seconds, summary FROM meetings ORDER BY date DESC LIMIT 20")
        meetings = [dict(row) for row in cur.fetchall()]

    db.close()

    if not meetings:
        click.echo("No meetings found.")
        return

    click.echo(f"Meetings ({len(meetings)}):\n")
    for m in meetings:
        slug = m.get("client_slug", "")
        dur = m.get('duration_seconds', 0)
        click.echo(f"  [{m['date']}] {slug} ({dur // 60}m {dur % 60}s)")
        if m.get("summary"):
            click.echo(f"    {m['summary'][:100]}")
        click.echo()


@cli.command()
@click.argument("client_slug")
@click.option("--output", "-o", default=None, help="Output directory (default: current dir)")
@click.pass_context
def export(ctx: click.Context, client_slug: str, output: str | None) -> None:
    """Export all meeting data for a client (AVG data subject access)."""
    config = ctx.obj["config"]
    from meetflow.storage.database import MeetingDB

    # Resolve the client's meetings via the DB client_slug column (the source of truth),
    # then map each row to its folder via json_path. Folder names are pure timestamps, so we
    # never match on a "_<slug>" suffix anymore.
    db = MeetingDB(config.data_dir / "meetflow.db")
    cur = db._conn.cursor()
    cur.execute("SELECT json_path FROM meetings WHERE client_slug = ?", (client_slug,))
    rows = cur.fetchall()
    db.close()

    client_dirs = sorted({Path(r["json_path"]).parent for r in rows if r["json_path"]})
    if not client_dirs:
        click.echo(f"No meetings found for client '{client_slug}'")
        return

    out_dir = Path(output) if output else Path(f"meetflow-export-{client_slug}")
    out_dir.mkdir(parents=True, exist_ok=True)

    exported = 0
    for src in client_dirs:
        if src.exists():
            shutil.copytree(str(src), str(out_dir / src.name), dirs_exist_ok=True)
            exported += 1

    click.echo(f"Exported {exported} meetings for '{client_slug}' to {out_dir}")


@cli.command()
@click.argument("client_slug")
@click.option("--confirm", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def delete(ctx: click.Context, client_slug: str, confirm: bool) -> None:
    """Delete all meeting data for a client (AVG right to erasure)."""
    config = ctx.obj["config"]
    from meetflow.storage.database import MeetingDB

    # Resolve via the DB client_slug column, then map rows to folders via json_path.
    db = MeetingDB(config.data_dir / "meetflow.db")
    cur = db._conn.cursor()
    cur.execute("SELECT id, json_path FROM meetings WHERE client_slug = ?", (client_slug,))
    rows = cur.fetchall()
    meeting_ids = [r["id"] for r in rows]
    client_dirs = sorted({Path(r["json_path"]).parent for r in rows if r["json_path"]})

    if not meeting_ids:
        click.echo(f"No meetings found for client '{client_slug}'")
        db.close()
        return

    if not confirm:
        click.echo(f"This will permanently delete {len(meeting_ids)} meetings for '{client_slug}':")
        for d in client_dirs:
            click.echo(f"  {d.name}")
        if not click.confirm("Proceed?"):
            click.echo("Cancelled.")
            db.close()
            return

    for mid in meeting_ids:
        cur.execute("DELETE FROM transcript_segments WHERE meeting_id = ?", (mid,))
        cur.execute("DELETE FROM action_items WHERE meeting_id = ?", (mid,))
        cur.execute("DELETE FROM meetings WHERE id = ?", (mid,))
    # Keep the FTS index consistent (the AFTER INSERT trigger doesn't cover deletes).
    cur.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
    db._conn.commit()
    db.close()

    for d in client_dirs:
        if d.exists():
            shutil.rmtree(str(d))

    click.echo(f"Deleted {len(meeting_ids)} meetings and DB records for '{client_slug}'")


@cli.command()
@click.option("--days", default=None, type=int, help="Override retention_days from config")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting")
@click.pass_context
def cleanup(ctx: click.Context, days: int | None, dry_run: bool) -> None:
    """Delete meetings older than retention period (AVG storage limitation)."""
    config = ctx.obj["config"]
    retention_days = days or config.privacy.retention_days
    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    meetings_dir = config.data_dir / "meetings"
    if not meetings_dir.exists():
        click.echo("No meetings found.")
        return

    old_dirs = []
    for d in sorted(meetings_dir.iterdir()):
        if not d.is_dir():
            continue
        date_part = d.name.split("_")[0] if "_" in d.name else ""
        if date_part and date_part < cutoff_str:
            old_dirs.append(d)

    if not old_dirs:
        click.echo(f"No meetings older than {retention_days} days (before {cutoff_str}).")
        return

    click.echo(f"{'Would delete' if dry_run else 'Deleting'} {len(old_dirs)} meetings older than {retention_days} days:\n")
    for d in old_dirs:
        click.echo(f"  {d.name}")

    if dry_run:
        return

    from meetflow.storage.database import MeetingDB

    db = MeetingDB(config.data_dir / "meetflow.db")
    cur = db._conn.cursor()
    cur.execute("SELECT id FROM meetings WHERE date < ?", (cutoff_str,))
    meeting_ids = [row["id"] for row in cur.fetchall()]
    for mid in meeting_ids:
        cur.execute("DELETE FROM transcript_segments WHERE meeting_id = ?", (mid,))
        cur.execute("DELETE FROM action_items WHERE meeting_id = ?", (mid,))
        cur.execute("DELETE FROM meetings WHERE id = ?", (mid,))
    db._conn.commit()
    db.close()

    for d in old_dirs:
        shutil.rmtree(str(d))

    click.echo(f"\nDeleted {len(old_dirs)} meetings and {len(meeting_ids)} database records.")


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Read-only preflight: are the binaries, models, sidecar, DB and calendar ready?"""
    config = ctx.obj["config"]
    checks: list[tuple[str, bool, str]] = []

    def _exists(p: str) -> bool:
        return bool(p) and Path(p).expanduser().exists()

    # whisper-cli + models
    checks.append(("whisper-cli", _exists(config.whisper.cli_path), config.whisper.cli_path or "(unset)"))
    checks.append(("whisper model", _exists(config.whisper.model_path), config.whisper.model_path or "(unset)"))
    if config.whisper.vad_enabled:
        checks.append(("VAD model", _exists(config.whisper.vad_model), config.whisper.vad_model or "(unset)"))
    # extraction CLI
    claude = shutil.which("claude")
    checks.append(("claude CLI", claude is not None, claude or "not on PATH"))
    # sidecar (.app) on macOS
    if sys.platform == "darwin":
        checks.append(("capture sidecar", _exists(config.capture.sidecar_path), config.capture.sidecar_path or "(unset)"))
    # data dir + DB
    meetings_dir = config.data_dir / "meetings"
    checks.append(("data dir writable", meetings_dir.exists(), str(config.data_dir)))
    try:
        from meetflow.storage.database import MeetingDB

        db = MeetingDB(config.data_dir / "meetflow.db")
        n = len(db.list_meetings())
        db.close()
        checks.append(("database", True, f"{n} meetings indexed"))
    except Exception as e:
        checks.append(("database", False, str(e)[:60]))
    # calendar (gws) — only if enabled
    if config.calendar.enabled:
        gws = shutil.which(config.calendar.gws_path) or _exists(config.calendar.gws_path)
        detail = "ready" if gws else f"{config.calendar.gws_path} not found"
        if gws:
            try:
                from meetflow.integrations.calendar import find_event

                find_event(config.calendar, datetime.now().strftime("%Y-%m-%d"), "00:00:00", 60)
                detail = "gws reachable"
            except Exception as e:  # pragma: no cover
                gws, detail = False, str(e)[:60]
        checks.append(("calendar (gws)", bool(gws), detail))
    # config sanity
    checks.append(("my_name set", bool(config.my_name), config.my_name or "(unset)"))
    checks.append(("glossary", bool(config.whisper.glossary), f"{len(config.whisper.glossary)} terms (advisory)"))

    hard_fail = False
    _echo("MeetFlow doctor:\n")
    for label, ok, detail in checks:
        # glossary is advisory-only, never a hard failure
        if not ok and label != "glossary":
            hard_fail = True
        mark = "OK  " if ok else "FAIL"
        _echo(f"  [{mark}] {label}: {detail}")
    _echo("")
    if hard_fail:
        _echo("Some checks failed.")
        sys.exit(1)
    _echo("All systems go.")


@cli.command()
@click.option("--meeting", "meeting_id", default=None, help="Backfill a single meeting (folder id)")
@click.option("--all-missing-titles", is_flag=True, help="Backfill every meeting with an empty title")
@click.pass_context
def backfill(ctx: click.Context, meeting_id: str | None, all_missing_titles: bool) -> None:
    """Re-extract missing titles for existing meetings and reconcile client_slug with the DB.

    Title-only by default (summaries are left untouched). Use for older recordings that were
    produced before calendar/title enrichment existed.
    """
    import json

    config = ctx.obj["config"]
    from meetflow.extract.llm import extract_meeting_data
    from meetflow.extract.schema import Meeting
    from meetflow.integrations.calendar import find_event
    from meetflow.storage.database import MeetingDB
    from meetflow.storage.files import save_meeting_json, save_meeting_markdown
    from meetflow.text import repair_text

    meetings_dir = config.data_dir / "meetings"
    if meeting_id:
        candidates = [meetings_dir / meeting_id]
    else:
        candidates = sorted(d for d in meetings_dir.iterdir() if d.is_dir() and (d / "meeting.json").exists())

    db = MeetingDB(config.data_dir / "meetflow.db")
    done = 0
    for mdir in candidates:
        jp = mdir / "meeting.json"
        if not jp.exists():
            click.echo(f"  skip {mdir.name}: no meeting.json")
            continue
        data = json.loads(jp.read_text(encoding="utf-8"))
        meeting = Meeting.model_validate(data)
        needs_title = not (meeting.meeting_title or "").strip()
        if all_missing_titles and not needs_title and not meeting_id:
            continue

        # Repair old-Windows mojibake + normalize dashes in stored text.
        meeting.meeting_title = repair_text(meeting.meeting_title)
        for seg in meeting.transcript:
            seg.text = repair_text(seg.text)
        ex = meeting.extraction
        ex.summary = repair_text(ex.summary)
        ex.client_needs = [repair_text(c) for c in ex.client_needs]
        ex.objections = [repair_text(o) for o in ex.objections]
        ex.follow_up_suggested = repair_text(ex.follow_up_suggested)
        for it in ex.action_items.i_owe_them + ex.action_items.they_owe_me:
            it.what = repair_text(it.what)
        for q in ex.quotes:
            q.text = repair_text(q.text)
            q.context = repair_text(q.context)

        # Reconcile slug from the DB column (source of truth).
        row = db._conn.execute("SELECT client_slug FROM meetings WHERE id = ?", (meeting.id,)).fetchone()
        if row and row["client_slug"]:
            meeting.client_slug = row["client_slug"]

        # Re-extract a title from the existing transcript (summary untouched).
        if needs_title and meeting.transcript:
            segs = [{"speaker": s.speaker, "start": s.start, "end": s.end, "text": s.text} for s in meeting.transcript]
            try:
                extraction = extract_meeting_data(segs, config.extraction, "", language=meeting.language or "nl")
                if extraction.meeting_title:
                    meeting.meeting_title = extraction.meeting_title
            except Exception as e:
                log.warning("backfill re-extract failed for %s: %s", meeting.id, e)

        # Optional calendar backfill of title/them.
        if config.calendar.enabled:
            cm = find_event(config.calendar, meeting.date, meeting.start_time or "00:00:00", meeting.duration_seconds)
            if cm:
                if not meeting.meeting_title and cm.title:
                    meeting.meeting_title = cm.title
                if not meeting.participants.them and cm.them_names:
                    meeting.participants.them = cm.them_names[0]

        save_meeting_json(meeting, mdir)
        save_meeting_markdown(meeting, mdir)
        rec = (data.get("recording") or {}).get("opus_path")
        db.index_meeting(meeting, str(jp), str(mdir / rec) if rec else None)
        click.echo(f"  backfilled {meeting.id}: title={meeting.meeting_title!r} slug={meeting.client_slug}")
        done += 1

    db.close()
    _rebuild_index(config)
    click.echo(f"Backfilled {done} meeting(s); INDEX.md rebuilt.")


@cli.command()
@click.option("--client", "client_slug", default=None, help="Default client slug for recordings")
@click.pass_context
def install(ctx: click.Context, client_slug: str | None) -> None:
    """Install MeetFlow as a background service that starts with Windows.

    After install, MeetFlow runs silently at boot. Double-tap Right Ctrl to record.
    """
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    if not pythonw.exists():
        # Fallback: try python.exe (will show console briefly)
        pythonw = Path(sys.executable)

    meetflow_dir = Path(__file__).parent.parent.resolve()
    startup = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"

    # Build the command: meetflow listen [--client slug]
    client_arg = f" {client_slug}" if client_slug else ""
    script = meetflow_dir / "meetflow" / "__main__.py"

    # VBScript wrapper: launches pythonw silently (no console window)
    vbs_content = (
        f'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.CurrentDirectory = "{meetflow_dir}"\n'
        f'WshShell.Run """{pythonw}"" ""{script}"" listen{client_arg}", 0, False\n'
    )

    vbs_path = startup / "MeetFlow.vbs"
    vbs_path.write_text(vbs_content, encoding="utf-8")

    click.echo(f"Installed: {vbs_path}")
    click.echo("MeetFlow will start silently at next login.")
    click.echo(f"  Working dir: {meetflow_dir}")
    click.echo(f"  Python: {pythonw}")
    if client_slug:
        click.echo(f"  Default client: {client_slug}")
    click.echo("\nDouble-tap Right Ctrl to start/stop recording. Tray icon shows status.")
    click.echo("To uninstall: meetflow uninstall")


@cli.command()
def uninstall() -> None:
    """Remove MeetFlow from Windows startup."""
    vbs_path = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup" / "MeetFlow.vbs"

    if vbs_path.exists():
        vbs_path.unlink()
        click.echo(f"Removed: {vbs_path}")
        click.echo("MeetFlow will no longer start at login.")
    else:
        click.echo("MeetFlow is not installed as a startup service.")
