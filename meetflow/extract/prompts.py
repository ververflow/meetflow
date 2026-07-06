"""Extraction prompt templates for meeting intelligence."""
from __future__ import annotations

SYSTEM_PROMPT = """\
You extract structured data from meeting transcripts. You are STRICTLY limited to information present in the transcript.

CRITICAL RULES:
- ONLY use information explicitly stated in the transcript. NEVER invent, assume, or infer content.
- If the transcript is too short or unclear to extract meaningful data, return empty fields — do NOT fabricate.
- Write in the same language as the transcript.
- Quotes must be VERBATIM from the transcript.
- Only include deadlines if EXPLICITLY mentioned with a date.
- If no action items are stated, return empty arrays.
- If no client needs are stated, return empty arrays.
- NEVER make up names, dates, or commitments that aren't in the transcript.
- The transcript uses channel-based speaker diarization. "Me" is the mic user, "Them" is the remote participant. Some attribution errors may remain — use conversational context to determine the actual speaker when labels seem inconsistent.
- In your OUTPUT, NEVER write the literal labels "Me" or "Them" (they are transcript markers, not names). Refer to the mic user as "the speaker" (or their name if known) and the other party as "the client" / "the other participant" (or their name), in the transcript's language.
"""

USER_PROMPT_TEMPLATE = """\
Extract structured data from this meeting transcript. ONLY use information from the transcript below. Do NOT add anything that isn't explicitly said.

{client_context}

## Transcript
{transcript}

Return valid JSON with this exact structure. Leave fields empty (empty string or empty array) if the transcript doesn't contain that information:

{{
  "meeting_title": "short descriptive title for this meeting (max 8 words)",
  "them_name": "name of the other participant if mentioned in the transcript, empty if unknown",
  "summary": "brief summary using ONLY what was said in the transcript",
  "client_needs": [],
  "action_items": {{
    "i_owe_them": [],
    "they_owe_me": []
  }},
  "quotes": [],
  "objections": [],
  "follow_up_suggested": ""
}}

Action item format: {{"what": "...", "deadline": null, "status": "open"}}
Quote format: {{"speaker": "me" or "them", "text": "VERBATIM quote from transcript", "context": "..."}}
"""


def build_extraction_prompt(transcript_text: str, client_context: str = "", language: str | None = None) -> str:
    """Build the full user prompt for extraction."""
    ctx = ""
    if client_context:
        ctx = f"## Client context\n{client_context}\n"

    prompt = USER_PROMPT_TEMPLATE.format(transcript=transcript_text, client_context=ctx)

    if language:
        lang_name = {"nl": "Dutch", "en": "English"}.get(language, language)
        prompt += (
            f"\n\nIMPORTANT: Write EVERY output value (meeting_title, summary, client_needs, "
            f"action items, objections, follow_up_suggested) in {lang_name}, matching the transcript."
        )
    return prompt


def format_transcript_for_prompt(segments: list[dict]) -> str:
    """Format transcript segments into readable text for the LLM prompt."""
    lines = []
    for seg in segments:
        speaker = "Me" if seg["speaker"] == "me" else "Them"
        timestamp = f"[{seg['start']:.0f}s]"
        lines.append(f"{timestamp} {speaker}: {seg['text']}")
    return "\n".join(lines)


# ── journal / brainstorm (lane C) ────────────────────────────────────────────────

JOURNAL_SYSTEM_PROMPT = """\
You distill a personal spoken journal / brainstorm into a faithful, structured note. The speaker is
alone, thinking out loud, and often switches between Dutch and English.

CRITICAL RULES:
- ONLY use what the speaker actually said. NEVER invent, assume, infer, or embellish.
- Write EVERY output value in the SAME language as the transcript (Dutch stays Dutch, English stays English).
- Be faithful and concise: capture the real content and feeling, not a paraphrase that adds meaning.
- The speaker sometimes addresses Claude / the assistant directly (e.g. "Hey Claude, ..."): collect
  those direct asks or messages under notes_to_claude, close to verbatim.
- todos are concrete things the speaker said they want or need to do. If none are stated, return [].
- If a section has nothing, return an empty array. Do NOT pad.
"""

JOURNAL_USER_TEMPLATE = """\
Distill this spoken personal journal / brainstorm into a structured note. ONLY use what is said below.

## Transcript
{transcript}

Return valid JSON with EXACTLY this structure. Leave a field empty ("" or []) when the transcript has nothing for it:

{{
  "title": "short descriptive title (max 8 words)",
  "summary": "faithful prose summary of what the speaker talked through",
  "themes": ["the main topics touched"],
  "insights": ["realizations or conclusions the speaker reached"],
  "decisions": ["decisions the speaker made out loud"],
  "open_questions": ["questions the speaker is still wrestling with"],
  "todos": ["concrete things the speaker said they want or need to do"],
  "notes_to_claude": ["any direct messages or asks the speaker addressed to Claude / the assistant"]
}}
"""


def build_journal_prompt(transcript_text: str, language: str | None = None) -> str:
    """Build the full user prompt for journal distillation."""
    prompt = JOURNAL_USER_TEMPLATE.format(transcript=transcript_text)
    if language:
        lang_name = {"nl": "Dutch", "en": "English"}.get(language, language)
        prompt += f"\n\nIMPORTANT: Write EVERY output value in {lang_name}, matching the transcript."
    return prompt


def format_journal_transcript(segments: list[dict]) -> str:
    """Format a solo transcript for the journal prompt — timestamped, NO speaker labels (the
    speaker is always the user, so 'Me:'/'Them:' would be noise)."""
    return "\n".join(f"[{seg['start']:.0f}s] {seg['text']}" for seg in segments)
