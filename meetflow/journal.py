"""Solo journaling / brainstorm pipeline (lane C).

A parallel, deliberately SIMPLER pipeline than the meeting one (cli._run_pipeline): mic-only audio,
no diarization (every segment is the speaker), no calendar / CRM / quarantine, and no action_items
rows. It reuses the meeting leaf functions (transcribe_audio, wav_to_opus, save_meeting_json,
MeetingDB) so it never touches meeting logic, and distills into a JournalExtraction stored in the
same MeetFlow store with kind='journal' plus its own JOURNAL.md index.

Why a separate module and not a branch inside _run_pipeline: journals must never destabilize the
proven meeting hot path, and bypassing transcribe_stereo structurally removes the false-"them"
segments the meeting pipeline produced on a solo recording.
"""
from __future__ import annotations

import dataclasses
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("meetflow")


def run_journal_pipeline(wav_path: Path, config):
    """Transcribe + distill a solo journal recording. Returns the Meeting (kind='journal') or None."""
    import numpy as np
    import soundfile as sf

    from meetflow.extract.llm import extract_journal_data
    from meetflow.extract.schema import Extraction, JournalExtraction, Meeting, Participants, Recording, TranscriptSegment
    from meetflow.storage.audio import wav_to_opus
    from meetflow.storage.database import MeetingDB
    from meetflow.storage.files import save_journal_markdown, save_meeting_json
    from meetflow.transcribe.engine import get_model, load_model, set_meeting_vocab, transcribe_audio

    # 1. Model (load if needed)
    try:
        get_model()
    except RuntimeError:
        load_model(config.whisper)

    # 2. Metadata from the folder name (YYYY-MM-DD_HHMM), mirroring the meeting pipeline.
    dir_name = wav_path.parent.name
    parts = dir_name.split("_")
    date_str = parts[0] if parts else datetime.now().strftime("%Y-%m-%d")
    time_str = parts[1] if len(parts) >= 2 else "0000"
    journal_id = dir_name

    info = sf.info(str(wav_path))
    duration_seconds = int(info.duration)
    if len(time_str) == 6:  # legacy HHMMSS
        start_time = f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"
    elif len(time_str) == 4:  # current HHMM
        start_time = f"{time_str[:2]}:{time_str[2:4]}:00"
    else:
        start_time = "00:00:00"
    try:
        start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M:%S")
        end_time = (start_dt + timedelta(seconds=duration_seconds)).strftime("%H:%M:%S")
    except ValueError:
        end_time = ""

    # 3. Load audio → mono mic channel (a journal WAV is already mono; guard for a stereo one).
    audio, _sr = sf.read(str(wav_path), dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]

    # 4. Transcribe: cli backend + VAD + auto nl/en, with the A/B-proven anti-loop override
    #    (max_context=0). No per-meeting vocab; the static glossary still primes via build_prompt.
    set_meeting_vocab([])
    jwhisper = dataclasses.replace(config.whisper, max_context=config.journal.max_context)
    segments = transcribe_audio(audio, jwhisper, None)  # None → auto within configured languages
    if not segments:
        # No speech (a mis-fired Hyper+J, a silent clip): discard the stray recording so nothing is
        # left behind. A journal has no quarantine concept and an empty one has zero value; unlike a
        # meeting, a solo silent mic clip can't be salvageable content. Only removes the folder if empty.
        log.info("No speech detected in journal recording — discarding.")
        try:
            wav_path.unlink(missing_ok=True)
            parent = wav_path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            log.warning("Failed to clean up empty journal recording", exc_info=True)
        return None
    log.info("Journal: %d segments transcribed", len(segments))

    # Correct the transcript-of-record with the shared fixups ("fair flow" -> VerverFlow, etc.).
    from meetflow.text import apply_fixups

    for s in segments:
        s.text = apply_fixups(s.text, config.whisper.fixups, config.whisper.fixups_brand)

    primary_lang = max(
        {s.language for s in segments},
        key=lambda lang: sum(1 for s in segments if s.language == lang),
    )

    # 5. Opus archive in the background (independent of the LLM distillation).
    opus_future = None
    opus_executor: ThreadPoolExecutor | None = None
    if config.storage.archive_format == "opus":
        opus_executor = ThreadPoolExecutor(max_workers=1)
        opus_future = opus_executor.submit(
            wav_to_opus, wav_path, config.storage.opus_bitrate, not config.storage.keep_wav
        )

    try:
        # 6. Distill via the Claude CLI. If distillation FAILS (e.g. the Claude usage limit), do NOT
        #    lose the transcript: fall back to an empty distillation, tag it, and still save + index.
        #    Re-run the distillation later (no re-record, no re-transcribe) with `meetflow redistill`.
        segs_for_llm = [{"speaker": "me", "start": s.start, "end": s.end, "text": s.text} for s in segments]
        try:
            journal = extract_journal_data(segs_for_llm, config.extraction, language=primary_lang)
            distill_ok = True
        except Exception as e:  # noqa: BLE001 — a failed distillation must not lose the transcript
            log.warning("Journal distillation failed (%s); saving transcript, re-run `meetflow redistill %s`", e, journal_id)
            journal = JournalExtraction(title="(distillatie mislukt)")
            distill_ok = False

        # 7. Build the Meeting (kind='journal'). extraction carries a MINIMAL summary/title so the
        #    DB row + FTS search stay uniform; the rich content lives in .journal. No action_items.
        meeting = Meeting(
            id=journal_id,
            client_slug="journal",
            kind="journal",
            meeting_title=journal.title,
            date=date_str,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=duration_seconds,
            language=primary_lang,
            participants=Participants(me=config.my_name),
            transcript=[TranscriptSegment(speaker="me", start=s.start, end=s.end, text=s.text) for s in segments],
            extraction=Extraction(meeting_title=journal.title, summary=journal.summary),
            journal=journal,
        )
        if not distill_ok:
            meeting.tags.append("distillatie-mislukt")

        opus_path = None
        if opus_future is not None:
            opus_path = opus_future.result()
            meeting.recording = Recording(
                opus_path=opus_path.name,
                opus_size_mb=round(opus_path.stat().st_size / (1024 * 1024), 2),
            )
    finally:
        if opus_executor is not None:
            opus_executor.shutdown(wait=False)

    # 8. Persist: meeting.json (uniform machine layer) + journal.md (human).
    journal_dir = wav_path.parent
    json_path = save_meeting_json(meeting, journal_dir)
    save_journal_markdown(meeting, journal_dir)

    # 9. Index (kind='journal'; extraction has no action_items so none are written).
    db = MeetingDB(config.data_dir / "meetflow.db")
    db.index_meeting(meeting, str(json_path), str(opus_path) if opus_path else None)
    db.close()

    # 10. Rebuild JOURNAL.md.
    try:
        _rebuild_journal_index(config)
    except Exception:  # noqa: BLE001
        log.warning("JOURNAL.md rebuild failed", exc_info=True)

    return meeting


def _rebuild_journal_index(config) -> Path:
    """Regenerate JOURNAL.md (the journal overview) from the database."""
    from meetflow.storage.database import MeetingDB
    from meetflow.storage.files import generate_journal_index

    db = MeetingDB(config.data_dir / "meetflow.db")
    journals = db.list_meetings(kind="journal")
    db.close()
    return generate_journal_index(journals, config.data_dir / config.journal.dirname)
