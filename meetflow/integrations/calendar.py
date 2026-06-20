"""Google Calendar enrichment via the local `gws` CLI (read-only).

Matches a recording to the calendar event that overlaps its time window, to fill the real
meeting title, the other participant's name, and a client slug. ONLY event metadata is read
through the already-authenticated `gws` CLI; recordings and transcripts never leave the
machine. Every failure path degrades to None/empty so the pipeline is never blocked.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from meetflow.config import CalendarConfig
from meetflow.text import normalize_dashes, sanitize_slug, slug_from_summary

log = logging.getLogger(__name__)


@dataclass
class CalendarMatch:
    title: str = ""
    them_names: list[str] = field(default_factory=list)
    slug_hint: str = ""
    attendee_emails: list[str] = field(default_factory=list)
    has_real_attendees: bool = False
    hangout_link: str = ""


def _rfc3339(dt: datetime) -> str:
    """Local-time RFC3339 with a colon in the UTC offset (Google wants +02:00, not +0200)."""
    s = dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    return (s[:-2] + ":" + s[-2:]) if len(s) >= 5 and (s[-5] in "+-") else s


def _query_events(cal: CalendarConfig, time_min: str, time_max: str) -> list[dict]:
    params = {
        "calendarId": cal.calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 10,
    }
    proc = subprocess.run(
        [cal.gws_path, "calendar", "events", "list", "--params", json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=cal.timeout_seconds,
    )
    if proc.returncode != 0:
        log.warning("gws calendar failed (%s): %s", proc.returncode, (proc.stderr or "")[-200:])
        return []
    # gws prints a 'Using keyring backend' banner to stderr; stdout is clean JSON.
    data = json.loads(proc.stdout or "{}")
    return data.get("items", []) or []


def _parse_dt(node: dict | None) -> datetime | None:
    """Parse an event start/end node to an aware datetime; None for all-day (date-only) events."""
    if not node:
        return None
    dt = node.get("dateTime")
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt)
    except ValueError:
        return None


def _build_match(cal: CalendarConfig, ev: dict) -> CalendarMatch:
    summary = normalize_dashes((ev.get("summary") or "").strip())
    my = {e.lower() for e in (cal.my_emails or [])}

    them_names: list[str] = []
    emails: list[str] = []
    for a in ev.get("attendees") or []:
        if a.get("self") or a.get("resource"):
            continue
        email = (a.get("email") or "").strip()
        if email.lower() in my:
            continue
        emails.append(email)
        name = (a.get("displayName") or "").strip() or (email.split("@")[0] if email else "")
        if name:
            them_names.append(name)

    has_real = bool(them_names)

    # Slug hint: domain map (reliable) -> attendee local-part -> summary first token.
    slug = ""
    for email in emails:
        domain = email.split("@")[-1].lower() if "@" in email else ""
        if domain and domain in (cal.domain_slugs or {}):
            slug = cal.domain_slugs[domain]
            break
    if not slug and emails and "@" in emails[0]:
        slug = sanitize_slug(emails[0].split("@")[0])
    if not slug and summary:
        slug = slug_from_summary(summary)

    return CalendarMatch(
        title=summary,
        them_names=them_names,
        slug_hint=slug,
        attendee_emails=emails,
        has_real_attendees=has_real,
        hangout_link=ev.get("hangoutLink") or "",
    )


def find_event(cal: CalendarConfig, date: str, start_time: str, duration_seconds: int) -> CalendarMatch | None:
    """Find the calendar event whose time window overlaps the recording. None if no match."""
    if not cal.enabled:
        return None
    try:
        rec_start = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M:%S").astimezone()
        rec_end = rec_start + timedelta(seconds=max(0, duration_seconds))
        tol = timedelta(minutes=cal.match_tolerance_minutes)
        q_min = _rfc3339(rec_start - timedelta(minutes=cal.lookback_minutes) - tol)
        q_max = _rfc3339(rec_end + timedelta(minutes=cal.lookahead_minutes) + tol)

        events = _query_events(cal, q_min, q_max)
        best, best_key = None, None
        for ev in events:
            ev_start, ev_end = _parse_dt(ev.get("start")), _parse_dt(ev.get("end"))
            if not ev_start or not ev_end:
                continue  # skip all-day events
            ws, we = ev_start - tol, ev_end + tol
            overlap = (min(rec_end, we) - max(rec_start, ws)).total_seconds()
            if overlap <= 0:
                continue
            rec_len = max(1.0, (rec_end - rec_start).total_seconds())
            ev_len = max(1.0, (ev_end - ev_start).total_seconds())
            score = overlap / min(rec_len, ev_len)
            delta = abs((ev_start - rec_start).total_seconds())
            key = (score, -delta)  # best overlap, then closest start
            if best_key is None or key > best_key:
                best, best_key = ev, key

        if best is None:
            log.info("No calendar event overlapping the recording window")
            return None
        match = _build_match(cal, best)
        log.info("Calendar match: %r (them=%s, slug=%s)", match.title, match.them_names, match.slug_hint)
        return match
    except Exception as e:  # never block the pipeline on calendar issues
        log.warning("calendar lookup failed: %s", e)
        return None


def get_calendar_context(cal: CalendarConfig, match: CalendarMatch | None) -> str:
    """Build a context block for the extraction prompt from a calendar match."""
    if not match:
        return ""
    parts = []
    if match.title:
        parts.append(f"Calendar event title: {match.title}")
    if match.them_names:
        parts.append(f"Attendees besides me: {', '.join(match.them_names)}")
    return "\n".join(parts)
