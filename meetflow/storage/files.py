"""Meeting file management — JSON and Markdown output."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from meetflow.extract.schema import Meeting

log = logging.getLogger(__name__)


def _meeting_title(json_path: str | None, fallback: str) -> str:
    """Read the meeting title from its meeting.json (falls back to the summary)."""
    if not json_path:
        return fallback
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return data.get("meeting_title") or (data.get("extraction") or {}).get("meeting_title") or fallback


def generate_index(meetings: list[dict], meetings_root: Path) -> Path:
    """Write INDEX.md — a reverse-chronological, scannable overview of all meetings.

    Pure derived data (from the DB + each meeting.json), so it is always safe to rebuild.
    """
    lines = [
        "# MeetFlow — meetings",
        "",
        f"_{len(meetings)} meetings · auto-generated, rebuild with `meetflow index`._",
        "",
        "| Datum | Titel | Duur | Open acties | Map |",
        "|---|---|---|---:|---|",
    ]
    for m in meetings:
        mid = m["id"]
        dur = m.get("duration_seconds") or 0
        title = _meeting_title(m.get("json_path"), m.get("summary") or "").strip().replace("\n", " ")
        if len(title) > 70:
            title = title[:69] + "…"
        open_a = m.get("open_actions") or 0
        lines.append(f"| {m.get('date', '')} | {title or '—'} | {dur // 60}m | {open_a} | [`{mid}`]({mid}/) |")
    lines.append("")

    meetings_root.mkdir(parents=True, exist_ok=True)
    index_path = meetings_root / "INDEX.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote INDEX.md (%d meetings)", len(meetings))
    return index_path


def save_meeting_json(meeting: Meeting, meeting_dir: Path) -> Path:
    """Write meeting.json to the meeting directory."""
    json_path = meeting_dir / "meeting.json"
    json_path.write_text(
        json.dumps(meeting.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Saved meeting.json to %s", json_path)
    return json_path


def save_meeting_markdown(meeting: Meeting, meeting_dir: Path) -> Path:
    """Write meeting.md — human-readable summary."""
    md_path = meeting_dir / "meeting.md"

    me_name = meeting.participants.me or "Me"
    them_name = meeting.participants.them or "Them"

    title = meeting.meeting_title or meeting.client_slug
    lines = [
        f"# Meeting: {title}",
        f"**Date:** {meeting.date} {meeting.start_time} - {meeting.end_time}",
        f"**Duration:** {meeting.duration_seconds // 60}m {meeting.duration_seconds % 60}s",
        f"**Language:** {meeting.language}",
        f"**Participants:** {me_name} + {them_name}",
        "",
        "## Summary",
        meeting.extraction.summary,
        "",
    ]

    if meeting.extraction.client_needs:
        lines.append("## Client Needs")
        for need in meeting.extraction.client_needs:
            lines.append(f"- {need}")
        lines.append("")

    if meeting.extraction.action_items.i_owe_them or meeting.extraction.action_items.they_owe_me:
        lines.append("## Action Items")
        if meeting.extraction.action_items.i_owe_them:
            lines.append(f"### {me_name}")
            for item in meeting.extraction.action_items.i_owe_them:
                deadline = f" (deadline: {item.deadline})" if item.deadline else ""
                lines.append(f"- [ ] {item.what}{deadline}")
        if meeting.extraction.action_items.they_owe_me:
            lines.append(f"### {them_name}")
            for item in meeting.extraction.action_items.they_owe_me:
                deadline = f" (deadline: {item.deadline})" if item.deadline else ""
                lines.append(f"- [ ] {item.what}{deadline}")
        lines.append("")

    if meeting.extraction.quotes:
        lines.append("## Quotes")
        for q in meeting.extraction.quotes:
            speaker = me_name if q.speaker == "me" else them_name
            lines.append(f'> "{q.text}" — {speaker}')
            if q.context:
                lines.append(f"> *Context: {q.context}*")
            lines.append("")

    if meeting.extraction.objections:
        lines.append("## Objections")
        for obj in meeting.extraction.objections:
            lines.append(f"- {obj}")
        lines.append("")

    if meeting.extraction.follow_up_suggested:
        lines.append("## Follow-up")
        lines.append(meeting.extraction.follow_up_suggested)
        lines.append("")

    if meeting.transcript:
        lines.append("## Transcript")
        for seg in meeting.transcript:
            speaker = me_name if seg.speaker == "me" else them_name
            lines.append(f"**[{seg.start:.0f}s] {speaker}:** {seg.text}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Saved meeting.md to %s", md_path)
    return md_path
