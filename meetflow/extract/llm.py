"""LLM client for meeting extraction — Claude API with Ollama fallback."""
from __future__ import annotations

import json
import logging
import os
import re

from meetflow.config import ExtractionConfig
from meetflow.extract.prompts import (
    JOURNAL_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_extraction_prompt,
    build_journal_prompt,
    format_journal_transcript,
    format_transcript_for_prompt,
)
from meetflow.extract.schema import ActionItem, ActionItems, Extraction, JournalExtraction, Quote

log = logging.getLogger(__name__)


def extract_meeting_data(
    segments: list[dict],
    config: ExtractionConfig,
    client_context: str = "",
    language: str | None = None,
) -> Extraction:
    """Run LLM extraction on transcript segments. Returns structured Extraction."""
    transcript_text = format_transcript_for_prompt(segments)

    # Short transcripts: skip LLM, just summarize directly
    if len(segments) == 0:
        return Extraction()
    if len(transcript_text) < 50:
        texts = " ".join(s["text"] for s in segments)
        return Extraction(summary=texts)

    user_prompt = build_extraction_prompt(transcript_text, client_context, language)

    if config.provider == "claude-code":
        raw = _call_claude_code(user_prompt, config)
    elif config.provider == "claude":
        raw = _call_claude(user_prompt, config)
    elif config.provider == "ollama":
        raw = _call_ollama(user_prompt, config)
    else:
        raise ValueError(f"Unknown extraction provider: {config.provider}")

    extraction = _parse_extraction(raw)

    # Validate: check quotes are actually from the transcript
    if extraction.quotes:
        transcript_lower = transcript_text.lower()
        valid_quotes = [q for q in extraction.quotes if q.text.lower()[:20] in transcript_lower]
        if len(valid_quotes) < len(extraction.quotes):
            log.warning("Removed %d hallucinated quotes", len(extraction.quotes) - len(valid_quotes))
            extraction.quotes = valid_quotes

    return extraction


def _call_claude_code(user_prompt: str, config: ExtractionConfig, system: str = SYSTEM_PROMPT) -> str:
    """Call Claude via the Claude Code CLI — uses existing subscription. Retries once on timeout."""
    import subprocess

    full_prompt = f"{system}\n\n{user_prompt}"

    # Map full model name to CLI short name (e.g. "claude-haiku-4-5-20251001" → "haiku")
    model = config.claude_model
    for short in ("haiku", "sonnet", "opus"):
        if short in model:
            model = short
            break

    for attempt in range(2):
        try:
            log.info("Calling Claude Code CLI for extraction (attempt %d, model=%s)...", attempt + 1, model)
            result = subprocess.run(
                ["claude", "-p", "--output-format", "json", "--model", model],
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
                creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW (Windows only)
            )

            if result.returncode != 0:
                log.warning("Claude Code CLI returned error: %s", result.stderr[:200])
                if attempt == 0:
                    continue
                raise RuntimeError(f"Claude Code CLI failed: {result.stderr[:200]}")

            response = json.loads(result.stdout)
            text = response.get("result", "")
            cost = response.get("total_cost_usd", 0)
            log.info("Claude Code extraction complete (%d chars, $%.4f)", len(text), cost)
            return text

        except subprocess.TimeoutExpired:
            log.warning("Claude Code CLI timed out (attempt %d)", attempt + 1)
            if attempt == 0:
                continue
            raise

    return ""


def _call_claude(user_prompt: str, config: ExtractionConfig, system: str = SYSTEM_PROMPT) -> str:
    """Call Claude API for extraction."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Set it in your environment or use provider='ollama'.")

    client = anthropic.Anthropic(api_key=api_key)

    log.info("Calling Claude (%s) for extraction...", config.claude_model)
    response = client.messages.create(
        model=config.claude_model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text
    log.info("Claude extraction complete (%d chars)", len(text))
    return text


def _call_ollama(user_prompt: str, config: ExtractionConfig, system: str = SYSTEM_PROMPT) -> str:
    """Call local Ollama for extraction."""
    import httpx

    log.info("Calling Ollama (%s) for extraction...", config.ollama_model)
    resp = httpx.post(
        f"{config.ollama_url}/api/generate",
        json={
            "model": config.ollama_model,
            "system": system,
            "prompt": user_prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.0,
                "num_predict": 2048,
            },
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    text = resp.json()["response"]
    log.info("Ollama extraction complete (%d chars)", len(text))
    return text


def extract_journal_data(
    segments: list[dict],
    config: ExtractionConfig,
    language: str | None = None,
) -> JournalExtraction:
    """Distill a solo journal / brainstorm transcript into a structured JournalExtraction.

    Reuses the meeting LLM plumbing (same providers, same JSON salvage) with the journal system
    prompt, so a Dutch journal stays Dutch and an English one stays English.
    """
    if not segments:
        return JournalExtraction()
    transcript_text = format_journal_transcript(segments)
    if len(transcript_text) < 50:
        return JournalExtraction(summary=" ".join(s["text"] for s in segments))

    user_prompt = build_journal_prompt(transcript_text, language)
    if config.provider == "claude-code":
        raw = _call_claude_code(user_prompt, config, system=JOURNAL_SYSTEM_PROMPT)
    elif config.provider == "claude":
        raw = _call_claude(user_prompt, config, system=JOURNAL_SYSTEM_PROMPT)
    elif config.provider == "ollama":
        raw = _call_ollama(user_prompt, config, system=JOURNAL_SYSTEM_PROMPT)
    else:
        raise ValueError(f"Unknown extraction provider: {config.provider}")
    return _parse_journal(raw)


def _parse_journal(raw: str) -> JournalExtraction:
    """Parse the LLM response into a JournalExtraction, salvaging loose JSON like meetings do."""
    data = _find_json_dict(raw)
    if data is not None:
        try:
            return JournalExtraction(
                title=_as_str(data.get("title")),
                summary=_as_str(data.get("summary")),
                themes=_as_str_list(data.get("themes")),
                insights=_as_str_list(data.get("insights")),
                decisions=_as_str_list(data.get("decisions")),
                open_questions=_as_str_list(data.get("open_questions")),
                todos=_as_str_list(data.get("todos")),
                notes_to_claude=_as_str_list(data.get("notes_to_claude")),
            )
        except Exception as e:  # noqa: BLE001 — last-resort guard around best-effort coercion
            log.warning("Journal coercion failed (%s); falling back to raw summary", e)
    log.warning("No parseable JSON in journal extraction. Raw: %s", raw.strip()[:300])
    return JournalExtraction(summary=raw.strip()[:500] if raw.strip() else "")


def _parse_extraction(raw: str) -> Extraction:
    """Parse the LLM response into an Extraction, salvaging as much as possible.

    Small models (haiku) vary run to run — they wrap JSON in prose, flip language, or
    get one nested field slightly wrong. Rather than discard everything on a single bad
    field, we locate the JSON object and coerce it field by field, dropping only the
    malformed pieces. Only a response with no recoverable JSON falls back to raw text.
    """
    data = _find_json_dict(raw)
    if data is not None:
        try:
            return _coerce_extraction(data)
        except Exception as e:  # noqa: BLE001 — last-resort guard around best-effort coercion
            log.warning("Extraction coercion failed (%s); falling back to raw summary", e)

    log.warning("No parseable JSON in LLM extraction. Raw: %s", raw.strip()[:300])
    return Extraction(summary=raw.strip()[:500] if raw.strip() else "No extraction available")


def _find_json_dict(raw: str) -> dict | None:
    """Locate and json.loads the extraction object from a possibly-wrapped response."""
    text = raw.strip()
    candidates = [text]

    code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if code_block:
        candidates.append(code_block.group(1).strip())

    brace_start, brace_end = text.find("{"), text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(text[brace_start : brace_end + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _as_str(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _loose_str(x) -> str:
    """Coerce a value to a meaningful string — including dicts the model over-structured."""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        for key in ("objection", "text", "what", "need", "value", "summary"):
            if x.get(key):
                return _as_str(x[key]).strip()
        return "; ".join(f"{k}: {v}" for k, v in x.items())
    return _as_str(x).strip()


def _as_str_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s for s in (_loose_str(x) for x in v) if s]


def _coerce_action_items(value) -> ActionItems:
    """Accept the {i_owe_them, they_owe_me} shape, a flat list, or loose items."""

    def items(raw_list) -> list[ActionItem]:
        out: list[ActionItem] = []
        for it in raw_list or []:
            if isinstance(it, dict) and it.get("what"):
                deadline = it.get("deadline")
                out.append(
                    ActionItem(
                        what=_as_str(it["what"]),
                        deadline=deadline if isinstance(deadline, str) and deadline else None,
                        status=_as_str(it.get("status")) or "open",
                    )
                )
            elif isinstance(it, str) and it.strip():
                out.append(ActionItem(what=it.strip()))
        return out

    if isinstance(value, dict):
        return ActionItems(i_owe_them=items(value.get("i_owe_them")), they_owe_me=items(value.get("they_owe_me")))
    if isinstance(value, list):
        # Model returned a flat list — keep the data rather than lose it.
        return ActionItems(i_owe_them=items(value))
    return ActionItems()


def _coerce_quotes(value) -> list[Quote]:
    out: list[Quote] = []
    for q in value or []:
        if isinstance(q, dict) and q.get("text"):
            out.append(Quote(speaker=_as_str(q.get("speaker")) or "them", text=_as_str(q["text"]), context=_as_str(q.get("context"))))
    return out


def _coerce_extraction(data: dict) -> Extraction:
    """Build a valid Extraction from a loosely-shaped dict, salvaging partial data."""
    return Extraction(
        meeting_title=_as_str(data.get("meeting_title")),
        them_name=_as_str(data.get("them_name")),
        summary=_as_str(data.get("summary")),
        client_needs=_as_str_list(data.get("client_needs")),
        action_items=_coerce_action_items(data.get("action_items")),
        quotes=_coerce_quotes(data.get("quotes")),
        objections=_as_str_list(data.get("objections")),
        follow_up_suggested=_as_str(data.get("follow_up_suggested")),
    )
