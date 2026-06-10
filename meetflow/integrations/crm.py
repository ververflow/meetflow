"""CRM integration — read/write client profile for any JSON-based CRM structure."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import portalocker

from meetflow.config import CRMConfig
from meetflow.extract.schema import Meeting

log = logging.getLogger(__name__)


def get_client_dir(crm: CRMConfig, slug: str) -> Path | None:
    """Find the client directory by slug. Returns None if CRM disabled or not found."""
    if not crm.enabled or crm.client_base is None:
        return None
    client_dir = crm.client_base / slug
    if client_dir.is_dir():
        return client_dir
    # Try case-insensitive match
    if crm.client_base.exists():
        for d in crm.client_base.iterdir():
            if d.is_dir() and d.name.lower() == slug.lower():
                return d
    return None


def read_profile(crm: CRMConfig, slug: str) -> dict | None:
    """Read a client's profile. Returns None if CRM disabled or not found."""
    client_dir = get_client_dir(crm, slug)
    if client_dir is None:
        return None

    profile_path = client_dir / crm.profile_path
    if not profile_path.exists():
        log.warning("Profile not found: %s", profile_path)
        return None

    return json.loads(profile_path.read_text(encoding="utf-8"))


def get_client_context(crm: CRMConfig, slug: str) -> str:
    """Build a context string from client profile for LLM prompts.

    Reads common fields from the profile JSON. Works with any structure
    that has top-level dicts like 'bedrijf'/'company', 'contact', 'context'.
    """
    profile = read_profile(crm, slug)
    if profile is None:
        return ""

    parts = []

    # Company info — try multiple field naming conventions
    company = profile.get("bedrijf", profile.get("company", {}))
    if company.get("naam") or company.get("name"):
        parts.append(f"Company: {company.get('naam') or company.get('name')}")
    if company.get("sector") or company.get("industry"):
        parts.append(f"Sector: {company.get('sector') or company.get('industry')}")

    # Contact info
    contact = profile.get("contact", {})
    if contact.get("naam") or contact.get("name"):
        parts.append(f"Contact: {contact.get('naam') or contact.get('name')}")
    if contact.get("functie") or contact.get("role"):
        parts.append(f"Role: {contact.get('functie') or contact.get('role')}")

    # Context / pain points
    context = profile.get("context", {})
    pain_points = context.get("pijnpunten", context.get("pain_points", []))
    if pain_points:
        parts.append(f"Known pain points: {', '.join(pain_points)}")
    situation = context.get("huidige_situatie", context.get("current_situation", ""))
    if situation:
        parts.append(f"Current situation: {situation}")

    # Active projects
    for proj in profile.get("projecten", profile.get("projects", [])):
        status = proj.get("status", "")
        if status not in ("afgerond", "completed", "done", ""):
            name = proj.get("naam", proj.get("name", "?"))
            parts.append(f"Active project: {name} (status: {status})")

    return "\n".join(parts)


def update_profile_with_meeting(crm: CRMConfig, meeting: Meeting, my_name: str = "") -> bool:
    """Append meeting activity and notes to the client's profile.

    Uses configurable field names from CRMConfig. Works with any JSON profile
    that has array fields for activities and notes.

    Returns True on success, False if CRM disabled or client not found.
    """
    client_dir = get_client_dir(crm, meeting.client_slug)
    if client_dir is None:
        log.info("CRM update skipped (disabled or client not found: %s)", meeting.client_slug)
        return False

    profile_path = client_dir / crm.profile_path
    if not profile_path.exists():
        log.warning("Cannot update CRM: no profile at %s", profile_path)
        return False

    # Backup before write
    backup_path = profile_path.with_suffix(".json.bak")
    shutil.copy2(profile_path, backup_path)

    with portalocker.Lock(str(profile_path), "r+", encoding="utf-8", timeout=5) as f:
        profile = json.loads(f.read())

        # Append to activity field
        activity_field = crm.activity_field
        if activity_field not in profile:
            profile[activity_field] = []
        profile[activity_field].append({
            "datum": meeting.date,
            "type": "meeting",
            "samenvatting": meeting.extraction.summary[:120],
        })

        # Append to notes field
        notes_field = crm.notes_field
        if notes_field not in profile:
            profile[notes_field] = []

        me_label = my_name or "me"
        actions = []
        for item in meeting.extraction.action_items.i_owe_them:
            dl = f", {item.deadline}" if item.deadline else ""
            actions.append(f"{item.what} ({me_label}{dl})")
        for item in meeting.extraction.action_items.they_owe_me:
            dl = f", {item.deadline}" if item.deadline else ""
            actions.append(f"{item.what} (client{dl})")

        action_str = f" Actions: {'; '.join(actions)}" if actions else ""
        profile[notes_field].append(
            f"{meeting.date}: Meeting — {meeting.extraction.summary[:80]}.{action_str}"
        )

        # Write back
        f.seek(0)
        f.truncate()
        f.write(json.dumps(profile, indent=2, ensure_ascii=False))

    log.info("Updated CRM profile for %s", meeting.client_slug)
    return True
