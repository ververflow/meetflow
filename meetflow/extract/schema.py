"""Pydantic models for structured meeting data."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    what: str
    deadline: str | None = None
    status: str = "open"


class Quote(BaseModel):
    speaker: str
    text: str
    context: str = ""


class ActionItems(BaseModel):
    i_owe_them: list[ActionItem] = Field(default_factory=list)
    they_owe_me: list[ActionItem] = Field(default_factory=list)


class Extraction(BaseModel):
    meeting_title: str = ""
    them_name: str = ""
    summary: str = ""
    client_needs: list[str] = Field(default_factory=list)
    action_items: ActionItems = Field(default_factory=ActionItems)
    quotes: list[Quote] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    follow_up_suggested: str = ""


class TranscriptSegment(BaseModel):
    speaker: str
    start: float
    end: float
    text: str


class Participants(BaseModel):
    me: str = ""
    them: str = ""


class Recording(BaseModel):
    opus_path: str | None = None
    opus_size_mb: float | None = None


class Meeting(BaseModel):
    id: str
    client_slug: str
    meeting_title: str = ""
    date: str
    start_time: str
    end_time: str
    duration_seconds: int
    language: str
    participants: Participants
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    extraction: Extraction = Field(default_factory=Extraction)
    recording: Recording = Field(default_factory=Recording)
    tags: list[str] = Field(default_factory=list)
    notes_user: str = ""
