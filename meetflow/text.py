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


def _is_word_char(c: str) -> bool:
    return bool(c) and c.isalnum()


def _ireplace(hay: str, needle: str, repl: str, prefix_only: bool = False) -> str:
    """Case-insensitive, boundary-aware substring replace (a port of dictation.lua's ireplace).

    Both-boundary by default: replaces only when the chars on BOTH sides of the match are
    non-word (or a string edge), so it never corrupts a longer correct word. prefix_only requires
    a boundary only BEFORE the match, so a brand glued into a Dutch compound ("houtcalcfacturen")
    still gets fixed. Matching is done against the lowercased original, so a replacement that
    contains the needle can't re-match.
    """
    if not needle:
        return hay
    lh, ln, n = hay.lower(), needle.lower(), len(hay)
    out: list[str] = []
    i = 0
    while True:
        s = lh.find(ln, i)
        if s == -1:
            out.append(hay[i:])
            break
        e = s + len(ln)  # exclusive end
        before_ok = not _is_word_char(lh[s - 1]) if s > 0 else True
        after_ok = True if prefix_only else (not _is_word_char(lh[e]) if e < n else True)
        if before_ok and after_ok:
            out.append(hay[i:s])
            out.append(repl)
        else:
            out.append(hay[i:e])
        i = e
    return "".join(out)


def apply_fixups(text: str, fixups=None, fixups_brand=None) -> str:
    """Apply post-transcription term corrections to a segment's text-of-record.

    `fixups` need a word boundary on BOTH sides (safe for ambiguous phrases like "fair flow");
    `fixups_brand` need one only BEFORE (so a brand inside a compound is still fixed). Mirrors the
    dictation.lua clean() pass so both lanes correct the same way. No-op when the lists are empty.
    """
    if not text:
        return text
    for pair in fixups or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            text = _ireplace(text, pair[0], pair[1], prefix_only=False)
    for pair in fixups_brand or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            text = _ireplace(text, pair[0], pair[1], prefix_only=True)
    return text


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
