"""The daemon runs the pipeline in a background thread, so the control loop stays responsive
(heartbeat + status keep updating, a start pressed mid-processing is refused) instead of blocking
for the whole multi-minute pipeline. This drives run_daemon with a mocked recorder + a slow fake
pipeline and asserts that behavior."""
from __future__ import annotations

import json
import threading
import time

from meetflow import daemon
from meetflow.config import Config


class _FakeRecorder:
    max_seconds = 9999
    elapsed_seconds = 0.0

    def __init__(self, config):
        pass

    def start(self, mic_only=False):
        pass

    def stop(self, subdir="meetings"):
        p = _TMP[0] / subdir / "2026-01-01_0000"
        p.mkdir(parents=True, exist_ok=True)
        return p / "recording.wav"


_TMP = [None]


def _read_state(status_path):
    try:
        return json.loads(status_path.read_text())["state"]
    except Exception:  # noqa: BLE001
        return None


def _wait_state(status_path, want, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if _read_state(status_path) == want:
            return True
        time.sleep(0.05)
    return False


def test_pipeline_runs_in_background_and_loop_stays_responsive(tmp_path, monkeypatch):
    _TMP[0] = tmp_path
    monkeypatch.setattr(daemon, "Recorder", _FakeRecorder)
    monkeypatch.setattr(daemon, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_notify_result", lambda *a, **k: None)

    cfg = Config(data_dir=tmp_path)
    started = threading.Event()
    calls = []

    def slow_pipeline(wav, config, slug):
        calls.append(wav)
        started.set()
        time.sleep(1.5)  # a "multi-minute" pipeline, compressed
        return None

    t = threading.Thread(target=daemon.run_daemon, args=(cfg, slow_pipeline, None), daemon=True)
    t.start()

    ctrl = cfg.data_dir / "control" / "command"
    status = cfg.data_dir / "control" / "status.json"

    assert _wait_state(status, "idle"), "daemon never reached idle"
    ctrl.write_text("start\n")
    assert _wait_state(status, "recording"), "start did not begin a recording"
    ctrl.write_text("stop\n")

    # The pipeline is now running on its own thread; the loop must NOT be blocked.
    assert started.wait(2.0), "pipeline never started"
    assert _wait_state(status, "processing", timeout=2.0)

    # A start pressed DURING processing must be refused (still processing, no second recording).
    ctrl.write_text("start\n")
    time.sleep(0.4)
    assert _read_state(status) == "processing", "loop wedged or accepted a start mid-processing"

    # Once the pipeline finishes we return to idle, and the mid-processing start was dropped.
    assert _wait_state(status, "idle", timeout=5.0), "did not return to idle after processing"
    time.sleep(0.4)
    assert calls == calls[:1], "pipeline ran more than once"
    assert len(calls) == 1, f"expected exactly one pipeline run, got {len(calls)}"
