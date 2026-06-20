"""Small, dependency-free text helpers shared across modules.

Kept as a leaf module (no intra-package imports) so both `cli.py` and the integration
modules can import it without an import cycle.
"""
from __future__ import annotations

import re

# Leading words on a calendar event title that are scheduling boilerplate, not the subject.
_SCHEDULING_WORDS = {
    "afspraak", "call", "meeting", "bespreking", "gesprek", "intake", "overleg",
    "met", "with", "the", "een", "sales", "follow", "up", "follow-up", "demo", "kennismaking",
}


def normalize_dashes(text: str) -> str:
    """Replace em/en dashes with a plain hyphen (house style: no fancy dashes in output)."""
    return text.replace("—", "-").replace("–", "-") if text else text


# Windows-1252-bytes-read-as-UTF-8 mojibake (from the old Windows MeetFlow). Longest first.
_MOJIBAKE = [
    ("â€™", "'"), ("â€˜", "'"), ("â€œ", '"'), ("â€\x9d", '"'), ("â€”", "-"),
    ("â€“", "-"), ("â€¦", "..."), ("Ã©", "é"), ("Ã«", "ë"), ("Ã¨", "è"),
    ("Ã¯", "ï"), ("Ã´", "ô"), ("Ã¶", "ö"), ("Ã¼", "ü"), ("Ã ", "à"),
    ("Ã§", "ç"), ("Â ", " "),
]


def repair_text(text: str) -> str:
    """Repair common mojibake and normalize dashes. Safe no-op on clean text.

    Targeted replacements only (no risky full re-decode), so correctly-encoded text is left
    untouched. Used when backfilling old Windows recordings.
    """
    if not text:
        return text
    if "â€" in text or "Ã" in text or "Â" in text:
        for bad, good in _MOJIBAKE:
            text = text.replace(bad, good)
    return normalize_dashes(text)


def sanitize_slug(slug: str) -> str:
    """Sanitize a client slug — only lowercase alphanumeric and hyphens."""
    clean = re.sub(r"[^a-z0-9-]", "-", slug.lower().strip())
    clean = re.sub(r"-+", "-", clean).strip("-")
    return clean[:50] if clean else "unknown"


def clean_event_summary(summary: str) -> str:
    """Strip leading scheduling boilerplate from a calendar title to get a subject hint.

    "Call Niels Koning - Burg Installatietechniek" -> "Niels Koning - Burg Installatietechniek".
    Returns the original (stripped) string if nothing recognizable is left.
    """
    if not summary:
        return ""
    tokens = summary.strip().split()
    i = 0
    while i < len(tokens) and tokens[i].lower().strip(":-") in _SCHEDULING_WORDS:
        i += 1
    cleaned = " ".join(tokens[i:]).strip()
    return cleaned or summary.strip()


def slug_from_summary(summary: str) -> str:
    """Derive a best-effort, single-token client slug from a calendar event title.

    Last-resort hint only (domain_slugs and attendee names take precedence). The user can
    always correct it with `meetflow tag`.
    """
    cleaned = clean_event_summary(summary)
    # First token of the subject (company or person name usually leads the title).
    first = re.split(r"[\s\-–—:|/]+", cleaned, maxsplit=1)[0].strip()
    return sanitize_slug(first) if first else ""
