"""Background daemon — watches a control file, drives record→pipeline, writes status.json.

The trigger (Ctrl+Alt+M) lives in Hammerspoon, which writes start/stop/toggle to the control
file and reads status.json for its menubar glyph. This keeps the flaky pynput listener out of
the picture: the daemon needs only Microphone (and, in Phase 4, Screen Recording) — never
Accessibility/Input-Monitoring, which was the root cause of "sometimes doesn't start".

A portalocker pidfile guarantees a single instance (so launchd KeepAlive can't double-spawn),
and a periodic heartbeat in the log + status.json makes "is it actually running?" answerable.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import portalocker

from meetflow.capture.recorder import Recorder

log = logging.getLogger("meetflow")

POLL_INTERVAL = 0.25
HEARTBEAT_INTERVAL = 30.0


# ── control-file / status paths ────────────────────────────────────────────────


def _control_dir(config) -> Path:
    d = config.data_dir / "control"
    d.mkdir(parents=True, exist_ok=True)
    return d


def command_path(config) -> Path:
    return _control_dir(config) / "command"


def status_path(config) -> Path:
    return _control_dir(config) / "status.json"


def pidfile_path(config) -> Path:
    return config.data_dir / "meetflow.pid"


def write_command(config, cmd: str) -> None:
    """Used by the `start`/`stop`/`toggle` CLI commands (Hammerspoon writes the file directly)."""
    command_path(config).write_text(cmd.strip() + "\n", encoding="utf-8")


def _consume_command(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not text:
        return None
    path.write_text("", encoding="utf-8")  # acknowledge
    return text.split()[-1].lower()  # last token = latest intent


def _write_status(config, state: str, since: float, last_meeting, elapsed: float = 0.0, kind: str = "meeting") -> None:
    status_path(config).write_text(
        json.dumps(
            {
                "state": state,
                "kind": kind,
                "since": since,
                "elapsed": round(elapsed, 1),
                "last_meeting": str(last_meeting) if last_meeting else None,
                "updated": time.time(),
            }
        ),
        encoding="utf-8",
    )


# ── notifications ──────────────────────────────────────────────────────────────


def _notify(title: str, message: str) -> None:
    from meetflow.notify import notify

    notify(title, message)


def _notify_result(meeting, meeting_dir) -> None:
    if meeting is not None:
        dur = meeting.duration_seconds
        n_seg = len(meeting.transcript)
        if getattr(meeting, "kind", "meeting") == "journal":
            label, body = "Journal", (meeting.extraction.summary or "")[:140]
        else:
            n_act = len(meeting.extraction.action_items.i_owe_them) + len(meeting.extraction.action_items.they_owe_me)
            label = "Meeting"
            body = (meeting.extraction.summary or "")[:140] + (f"\n{n_act} actiepunten" if n_act else "")
        _notify(f"{label} opgeslagen ({dur // 60}m {dur % 60}s, {n_seg} segmenten)", body)
    else:
        _notify("Opname opgeslagen", "Geen spraak gedetecteerd")
    if meeting_dir:
        opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
        try:
            subprocess.run([opener, str(meeting_dir)])
        except Exception:  # noqa: BLE001
            pass


# ── main loop ──────────────────────────────────────────────────────────────────


def run_daemon(config, run_pipeline, run_journal=None) -> None:
    """Main loop. `run_pipeline(wav, config, client_slug)` (meetings) and the optional
    `run_journal(wav, config)` (lane C) are injected from cli.py."""
    pidfile = pidfile_path(config)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    lock = open(pidfile, "a")  # noqa: SIM115 — "a" so a second instance probing it can't truncate our pid
    try:
        portalocker.lock(lock, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.LockException:
        log.error("Another MeetFlow daemon is already running (pidfile %s locked). Exiting.", pidfile)
        lock.close()
        return
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    recorder = Recorder(config)
    state = "idle"
    session_kind = "meeting"  # what the in-flight recording is: "meeting" or "journal"
    since = time.time()
    last_meeting = None
    last_heartbeat = 0.0
    job_thread: threading.Thread | None = None  # background pipeline, so the loop stays responsive
    job_result: dict = {}

    _consume_command(command_path(config))  # drop any stale command
    _write_status(config, state, since, last_meeting, kind=session_kind)
    log.info("MeetFlow daemon ready (control=%s). Meeting=Ctrl+Alt+M, journal=Hyper+J.", _control_dir(config))

    def do_start(kind: str = "meeting") -> None:
        nonlocal state, since, session_kind
        if state != "idle":
            return
        if kind == "journal" and run_journal is None:
            log.warning("Journal start requested but no journal pipeline injected; ignoring")
            return
        try:
            recorder.start(mic_only=(kind == "journal"))
        except Exception as e:  # noqa: BLE001 — a failed mic start must not kill the daemon
            log.exception("Failed to start recording")
            _notify("MeetFlow fout", f"Opname starten faalde (microfoon-toegang?): {e}"[:200])
            return
        state, since, session_kind = "recording", time.time(), kind
        _write_status(config, state, since, last_meeting, kind=session_kind)
        log.info("%s recording started", kind.capitalize())

    def do_stop() -> None:
        nonlocal state, since, job_thread, job_result
        if state != "recording":
            return
        kind = session_kind
        # recorder.stop() (buffered-audio → WAV) is fast and resets the recorder, so do it inline;
        # only the multi-minute PIPELINE moves to a thread, so heartbeat + status + command polling
        # keep running (the menubar shows ⏳ instead of flipping to "down").
        subdir = config.journal.dirname if kind == "journal" else "meetings"
        wav = recorder.stop(subdir=subdir)
        state = "processing"
        _write_status(config, state, since, last_meeting, kind=kind)
        log.info("Recording stopped, processing (%s) in background...", kind)
        job_result = {"kind": kind, "dir": (wav.parent if wav else last_meeting)}

        def job() -> None:
            try:
                if not wav:
                    job_result["meeting"] = None
                elif kind == "journal":
                    job_result["meeting"] = run_journal(wav, config) if run_journal else None
                else:
                    job_result["meeting"] = run_pipeline(wav, config, None)
            except Exception as e:  # noqa: BLE001 — surfaced by finish_job on the main thread
                log.exception("Pipeline failed")
                job_result["error"] = e

        job_thread = threading.Thread(target=job, name="meetflow-pipeline", daemon=True)
        job_thread.start()

    def finish_job() -> None:
        """Called by the main loop once the background pipeline thread has finished."""
        nonlocal state, since, last_meeting, job_thread
        if job_result.get("dir"):
            last_meeting = job_result["dir"]
        if job_result.get("error") is not None:
            _notify("MeetFlow fout", str(job_result["error"])[:200])
        else:
            _notify_result(job_result.get("meeting"), last_meeting)
        job_thread = None
        state, since = "idle", time.time()
        _write_status(config, state, since, last_meeting, kind=job_result.get("kind", "meeting"))
        # Drop any command that arrived DURING processing: otherwise a toggle/journal pressed while
        # the pipeline ran would fire the instant we return to idle — a surprise start.
        _consume_command(command_path(config))
        log.info("Ready for next recording")

    try:
        while True:
            # Reap a finished background pipeline first (returns us to idle + notifies).
            if state == "processing" and job_thread is not None and not job_thread.is_alive():
                finish_job()

            cmd = _consume_command(command_path(config))
            if cmd == "toggle":
                cmd = "stop" if state == "recording" else ("start" if state == "idle" else None)
            if cmd == "start":
                do_start("meeting")
            elif cmd == "journal":
                # Toggle: stop a running journal, or start one when idle. Refuses (no-op) while a
                # MEETING is recording — do_start guards on state != idle.
                if state == "recording" and session_kind == "journal":
                    do_stop()
                else:
                    do_start("journal")
            elif cmd == "stop":
                do_stop()

            if state == "recording" and recorder.elapsed_seconds > recorder.max_seconds:
                log.info("Max duration (%.0f min) reached — auto-stopping", recorder.max_seconds / 60)
                do_stop()

            now = time.time()
            if now - last_heartbeat > HEARTBEAT_INTERVAL:
                last_heartbeat = now
                elapsed = recorder.elapsed_seconds if state == "recording" else 0.0
                _write_status(config, state, since, last_meeting, elapsed, kind=session_kind)
                # DEBUG, not INFO: heartbeats were 97.8% of the log. status.json still updates every
                # 30s (the menubar liveness check), and real state changes log at INFO in do_start/stop.
                log.debug("[heartbeat] state=%s kind=%s", state, session_kind)

            time.sleep(POLL_INTERVAL)
    finally:
        portalocker.unlock(lock)
        lock.close()
