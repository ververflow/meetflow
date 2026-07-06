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


_VENTURE_LABELS = {
    "houtcalc": "HoutCalc",
    "ververflow": "Agency (VerverFlow)",
    "creator-partnerships": "Creator Partnerships (gearchiveerd)",  # retired venture; kept for history
    "": "Overig / ongetagd",
}
_VENTURE_ORDER = ["houtcalc", "ververflow", "creator-partnerships", ""]


def generate_index(meetings: list[dict], meetings_root: Path, quarantine_dirname: str = "_quarantine") -> Path:
    """Write INDEX.md — a scannable overview of all meetings, GROUPED PER VENTURE (HoutCalc / Agency
    / Overig), each row showing the interaction type + counterparty.

    Pure derived data (from the DB + each meeting.json), so it is always safe to rebuild.
    Quarantined meetings (folder under `quarantine_dirname`) are excluded but counted in the footer.
    """
    listed, quarantined = [], 0
    for m in meetings:
        jp = m.get("json_path") or ""
        if jp and quarantine_dirname in Path(jp).parts:
            quarantined += 1
        else:
            listed.append(m)

    by_venture: dict[str, list[dict]] = {}
    for m in listed:
        by_venture.setdefault(m.get("venture") or "", []).append(m)

    lines = [
        "# MeetFlow — meetings",
        "",
        f"_{len(listed)} meetings · gegroepeerd per venture · rebuild with `meetflow index`._",
        "",
    ]
    for v in _VENTURE_ORDER + [k for k in by_venture if k not in _VENTURE_ORDER]:
        rows = by_venture.get(v)
        if not rows:
            continue
        lines += [f"## {_VENTURE_LABELS.get(v, v or 'Overig')} · {len(rows)}", "",
                  "| Datum | Type | Titel | Met | Acties | Map |", "|---|---|---|---|---:|---|"]
        for m in rows:
            mid = m["id"]
            title = _meeting_title(m.get("json_path"), m.get("summary") or "").strip().replace("\n", " ")
            if len(title) > 58:
                title = title[:57] + "…"
            lines.append(
                f"| {m.get('date', '')} | {m.get('type') or '—'} | {title or '—'} | "
                f"{m.get('client_slug') or '—'} | {m.get('open_actions') or 0} | [`{mid}`]({mid}/) |"
            )
        lines.append("")
    if quarantined:
        lines.append(f"_{quarantined} in quarantaine (zie `{quarantine_dirname}/`)._")
        lines.append("")

    meetings_root.mkdir(parents=True, exist_ok=True)
    index_path = meetings_root / "INDEX.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote INDEX.md (%d meetings, %d quarantined)", len(listed), quarantined)
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
            lines.append(f'> "{q.text}" ({speaker})')
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


# ── journal (lane C) ─────────────────────────────────────────────────────────────


def save_journal_markdown(meeting: Meeting, journal_dir: Path) -> Path:
    """Write journal.md — the human-readable distillation of a solo journaling session. No speaker
    labels (the speaker is always the user); todos render as checkboxes."""
    md_path = journal_dir / "journal.md"
    j = meeting.journal
    title = (j.title if j else "") or meeting.meeting_title or "Journal"
    lines = [
        f"# Journal: {title}",
        f"**Date:** {meeting.date} {meeting.start_time} - {meeting.end_time}",
        f"**Duration:** {meeting.duration_seconds // 60}m {meeting.duration_seconds % 60}s",
        f"**Language:** {meeting.language}",
        "",
        "## Summary",
        (j.summary if j else "") or meeting.extraction.summary,
        "",
    ]
    if j:
        for header, items in [
            ("Themes", j.themes),
            ("Insights", j.insights),
            ("Decisions", j.decisions),
            ("Open questions", j.open_questions),
            ("Todos", j.todos),
            ("Notes to Claude", j.notes_to_claude),
        ]:
            if items:
                lines.append(f"## {header}")
                prefix = "- [ ] " if header == "Todos" else "- "
                lines.extend(f"{prefix}{it}" for it in items)
                lines.append("")

    if meeting.transcript:
        lines.append("## Transcript")
        for seg in meeting.transcript:
            lines.append(f"**[{seg.start:.0f}s]** {seg.text}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Saved journal.md to %s", md_path)
    return md_path


def generate_journal_index(journals: list[dict], journal_root: Path) -> Path:
    """Write JOURNAL.md — a reverse-chronological overview of journaling sessions (lane C).
    Pure derived data from the DB; safe to rebuild. No open-actions column (journals hold none)."""
    lines = [
        "# MeetFlow — journal",
        "",
        f"_{len(journals)} entries · auto-generated._",
        "",
        "| Datum | Titel | Duur | Map |",
        "|---|---|---|---|",
    ]
    for m in journals:
        mid = m["id"]
        dur = m.get("duration_seconds") or 0
        title = _meeting_title(m.get("json_path"), m.get("summary") or "").strip().replace("\n", " ")
        if len(title) > 70:
            title = title[:69] + "…"
        lines.append(f"| {m.get('date', '')} | {title or '—'} | {dur // 60}m | [`{mid}`]({mid}/) |")
    lines.append("")

    journal_root.mkdir(parents=True, exist_ok=True)
    index_path = journal_root / "JOURNAL.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote JOURNAL.md (%d entries)", len(journals))
    return index_path
