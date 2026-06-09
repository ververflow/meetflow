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


def _write_status(config, state: str, since: float, last_meeting, elapsed: float = 0.0) -> None:
    status_path(config).write_text(
        json.dumps(
            {
                "state": state,
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
        n_act = len(meeting.extraction.action_items.i_owe_them) + len(meeting.extraction.action_items.they_owe_me)
        body = (meeting.extraction.summary or "")[:140] + (f"\n{n_act} actiepunten" if n_act else "")
        _notify(f"Meeting opgeslagen ({dur // 60}m {dur % 60}s, {n_seg} segmenten)", body)
    else:
        _notify("Meeting opgeslagen", "Geen spraak gedetecteerd")
    if meeting_dir:
        opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
        try:
            subprocess.run([opener, str(meeting_dir)])
        except Exception:  # noqa: BLE001
            pass


# ── main loop ──────────────────────────────────────────────────────────────────


def run_daemon(config, run_pipeline) -> None:
    """Main loop. `run_pipeline(wav_path, config, client_slug)` is injected from cli.py."""
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
    since = time.time()
    last_meeting = None
    last_heartbeat = 0.0

    _consume_command(command_path(config))  # drop any stale command
    _write_status(config, state, since, last_meeting)
    log.info("MeetFlow daemon ready (control=%s). Trigger via Ctrl+Alt+M.", _control_dir(config))

    def do_start() -> None:
        nonlocal state, since
        if state != "idle":
            return
        try:
            recorder.start()
        except Exception as e:  # noqa: BLE001 — a failed mic start must not kill the daemon
            log.exception("Failed to start recording")
            _notify("MeetFlow fout", f"Opname starten faalde (microfoon-toegang?): {e}"[:200])
            return
        state, since = "recording", time.time()
        _write_status(config, state, since, last_meeting)
        log.info("Recording started")

    def do_stop() -> None:
        nonlocal state, since, last_meeting
        if state != "recording":
            return
        state = "processing"
        _write_status(config, state, since, last_meeting)
        log.info("Recording stopped, processing...")
        try:
            wav = recorder.stop()
            meeting = run_pipeline(wav, config, None) if wav else None
            if wav:
                last_meeting = wav.parent
            _notify_result(meeting, last_meeting)
        except Exception as e:  # noqa: BLE001
            log.exception("Pipeline failed")
            _notify("MeetFlow fout", str(e)[:200])
        state, since = "idle", time.time()
        _write_status(config, state, since, last_meeting)
        log.info("Ready for next recording")

    try:
        while True:
            cmd = _consume_command(command_path(config))
            if cmd == "toggle":
                cmd = "stop" if state == "recording" else ("start" if state == "idle" else None)
            if cmd == "start":
                do_start()
            elif cmd == "stop":
                do_stop()

            if state == "recording" and recorder.elapsed_seconds > recorder.max_seconds:
                log.info("Max duration (%.0f min) reached — auto-stopping", recorder.max_seconds / 60)
                do_stop()

            now = time.time()
            if now - last_heartbeat > HEARTBEAT_INTERVAL:
                last_heartbeat = now
                elapsed = recorder.elapsed_seconds if state == "recording" else 0.0
                _write_status(config, state, since, last_meeting, elapsed)
                log.info("[heartbeat] state=%s", state)

            time.sleep(POLL_INTERVAL)
    finally:
        portalocker.unlock(lock)
        lock.close()
