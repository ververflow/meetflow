# Contributing

Personal tool, but kept clean enough to hand to a contributor (human or AI).

## Local setup (macOS, Apple Silicon)

```sh
cd ~/code/tools/meetflow
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Requires:
- macOS 14.4+ on Apple Silicon
- `whisper-cpp` (whisper-cli + whisper-server) and `ffmpeg` via Homebrew, on PATH
- The large-v3 model + the Silero VAD model under `~/.local/share/whisper-models/`
  (see README "Setup")
- The [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) on PATH, authenticated
  (default `claude-code` extraction provider), or a local Ollama (`provider = "ollama"`)

## Configure locally without polluting git

```sh
cp meetflow.toml meetflow.local.toml   # set my_name + absolute model paths; it overrides meetflow.toml
```

`*.local.toml`, `data/`, `*.opus`, `*.wav`, `*.log` are gitignored — confidential meeting data
never enters git (it lives in `~/Library/Application Support/MeetFlow`, outside this repo).

## Tests + lint

```sh
.venv/bin/pytest -q
.venv/bin/ruff check meetflow tests
```

## Code style

- Python 3.11+ (`from __future__ import annotations`, `|` unions)
- Ruff for formatting/linting (config in `pyproject.toml`), ~120-char lines
- Pydantic v2 for all data models — never define a meeting/extraction shape outside
  `meetflow/extract/schema.py`
- Keep `diarize.py` / `filters.py` backend-agnostic: the transcription engine
  (`transcribe/engine.py`) hides whether it used whisper-cli or the HTTP server behind the
  `transcribe_audio(audio, config, language) -> list[Segment]` contract.

## Submitting changes

1. Branch, make the change.
2. `pytest -q` must pass, `ruff check` clean.
3. If you touch the recording/transcription pipeline, do at least one end-to-end run with
   `meetflow process <a short wav>` and confirm the transcript + extraction look right.

## Out of scope

- Cloud upload / server-side anything — local-only by design.
- Replacing large-v3 — it's the accuracy sweet spot on this hardware (whisper-cli + Metal).
- Removing the Claude CLI extraction path (the no-API-key story is core).
