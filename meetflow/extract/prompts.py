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
