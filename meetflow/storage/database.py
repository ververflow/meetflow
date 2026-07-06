"""SQLite + FTS5 storage for meeting search and metadata."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from meetflow.extract.schema import Meeting

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    client_slug TEXT,
    kind TEXT DEFAULT 'meeting',
    date TEXT,
    duration_seconds INTEGER,
    summary TEXT,
    json_path TEXT,
    audio_path TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    rowid INTEGER PRIMARY KEY,
    meeting_id TEXT REFERENCES meetings(id),
    speaker TEXT,
    start_sec REAL,
    end_sec REAL,
    text TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    meeting_id,
    speaker,
    text,
    content='transcript_segments',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS transcript_ai AFTER INSERT ON transcript_segments BEGIN
    INSERT INTO transcript_fts(rowid, meeting_id, speaker, text)
    VALUES (new.rowid, new.meeting_id, new.speaker, new.text);
END;

-- External-content FTS needs explicit delete/update sync (the 'delete' command feeds the OLD row
-- so FTS can remove its tokens). Without these, index_meeting's delete+reinsert of a meeting's
-- segments (and `cleanup`) orphaned rows in transcript_fts, so search returned stale snippets.
CREATE TRIGGER IF NOT EXISTS transcript_ad AFTER DELETE ON transcript_segments BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, meeting_id, speaker, text)
    VALUES ('delete', old.rowid, old.meeting_id, old.speaker, old.text);
END;

CREATE TRIGGER IF NOT EXISTS transcript_au AFTER UPDATE ON transcript_segments BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, meeting_id, speaker, text)
    VALUES ('delete', old.rowid, old.meeting_id, old.speaker, old.text);
    INSERT INTO transcript_fts(rowid, meeting_id, speaker, text)
    VALUES (new.rowid, new.meeting_id, new.speaker, new.text);
END;

CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY,
    meeting_id TEXT REFERENCES meetings(id),
    direction TEXT,
    what TEXT,
    deadline TEXT,
    status TEXT DEFAULT 'open',
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_meetings_client ON meetings(client_slug);
CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(date);
CREATE INDEX IF NOT EXISTS idx_actions_status ON action_items(status);
"""


class MeetingDB:
    """SQLite database for meeting metadata and full-text search."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        self._conn.executescript(_SCHEMA)
        log.info("Database ready at %s", db_path)

    def _migrate(self) -> None:
        """Run schema migrations on existing databases."""
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(meetings)")
        columns = [row[1] for row in cur.fetchall()]
        if "duration_minutes" in columns and "duration_seconds" not in columns:
            cur.execute("ALTER TABLE meetings RENAME COLUMN duration_minutes TO duration_seconds")
            self._conn.commit()
            log.info("Migrated: duration_minutes -> duration_seconds")
        # `columns` is non-empty only when the meetings table already exists; a fresh DB gets `kind`
        # straight from _SCHEMA below, so this ALTER runs only on pre-journal databases.
        if columns and "kind" not in columns:
            cur.execute("ALTER TABLE meetings ADD COLUMN kind TEXT DEFAULT 'meeting'")
            self._conn.commit()
            log.info("Migrated: added meetings.kind (default 'meeting')")
        # One-time FTS repair (user_version-gated so it runs once): index_meeting deletes+reinserts
        # a meeting's segments with fresh rowids, and before the AFTER DELETE/UPDATE triggers existed
        # that orphaned rows in transcript_fts. Rebuild heals any existing drift.
        cur.execute("PRAGMA user_version")
        if cur.fetchone()[0] < 1:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transcript_fts'")
            if cur.fetchone():
                try:
                    self._conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
                    log.info("Migrated: rebuilt transcript_fts")
                except sqlite3.Error:
                    log.warning("FTS rebuild skipped", exc_info=True)
            cur.execute("PRAGMA user_version = 1")
            self._conn.commit()

    def index_meeting(self, meeting: Meeting, json_path: str, audio_path: str | None = None) -> None:
        """Insert or replace a meeting and its transcript segments."""
        cur = self._conn.cursor()

        cur.execute(
            "INSERT OR REPLACE INTO meetings (id, client_slug, kind, date, duration_seconds, summary, json_path, audio_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (meeting.id, meeting.client_slug, getattr(meeting, "kind", "meeting"), meeting.date, meeting.duration_seconds, meeting.extraction.summary, json_path, audio_path),
        )

        # Clear old segments for re-indexing
        cur.execute("DELETE FROM transcript_segments WHERE meeting_id = ?", (meeting.id,))

        for seg in meeting.transcript:
            cur.execute(
                "INSERT INTO transcript_segments (meeting_id, speaker, start_sec, end_sec, text) VALUES (?, ?, ?, ?, ?)",
                (meeting.id, seg.speaker, seg.start, seg.end, seg.text),
            )

        # Index action items
        cur.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting.id,))
        for item in meeting.extraction.action_items.i_owe_them:
            cur.execute(
                "INSERT INTO action_items (meeting_id, direction, what, deadline, status) VALUES (?, ?, ?, ?, ?)",
                (meeting.id, "i_owe_them", item.what, item.deadline, item.status),
            )
        for item in meeting.extraction.action_items.they_owe_me:
            cur.execute(
                "INSERT INTO action_items (meeting_id, direction, what, deadline, status) VALUES (?, ?, ?, ?, ?)",
                (meeting.id, "they_owe_me", item.what, item.deadline, item.status),
            )

        self._conn.commit()
        log.info("Indexed meeting %s (%d segments, %d actions)", meeting.id, len(meeting.transcript), len(meeting.extraction.action_items.i_owe_them) + len(meeting.extraction.action_items.they_owe_me))

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across all transcripts. Returns matching meetings with snippets."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.client_slug, m.date, m.summary,
                   snippet(transcript_fts, 2, '>>>', '<<<', '...', 32) as snippet,
                   f.speaker
            FROM transcript_fts f
            JOIN meetings m ON m.id = f.meeting_id
            WHERE transcript_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    def list_meetings(self, kind: str = "meeting") -> list[dict]:
        """Meetings of the given kind, newest first, with open-action counts (for the overview).

        Defaults to "meeting" so INDEX.md excludes journals; JOURNAL.md passes kind="journal".
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.client_slug, m.date, m.duration_seconds, m.summary, m.json_path,
                   (SELECT COUNT(*) FROM action_items a
                    WHERE a.meeting_id = m.id AND a.status = 'open') AS open_actions
            FROM meetings m
            WHERE m.kind = ?
            ORDER BY m.id DESC
            """,
            (kind,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_meetings_by_client(self, client_slug: str) -> list[dict]:
        """Get all meetings for a client, most recent first."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, date, duration_seconds, summary FROM meetings WHERE client_slug = ? ORDER BY date DESC",
            (client_slug,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_open_actions(self, client_slug: str | None = None) -> list[dict]:
        """Get all open action items, optionally filtered by client."""
        cur = self._conn.cursor()
        if client_slug:
            cur.execute(
                """
                SELECT a.*, m.client_slug, m.date as meeting_date
                FROM action_items a JOIN meetings m ON a.meeting_id = m.id
                WHERE a.status = 'open' AND m.client_slug = ?
                ORDER BY a.deadline
                """,
                (client_slug,),
            )
        else:
            cur.execute(
                """
                SELECT a.*, m.client_slug, m.date as meeting_date
                FROM action_items a JOIN meetings m ON a.meeting_id = m.id
                WHERE a.status = 'open'
                ORDER BY a.deadline
                """,
            )
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
