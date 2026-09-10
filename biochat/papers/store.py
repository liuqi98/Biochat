"""Transactional page checkpoints and a durable paper-change inbox."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


class PaperStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                profile TEXT NOT NULL, source TEXT NOT NULL, completed_until TEXT,
                window_start TEXT, window_end TEXT, cursor TEXT,
                PRIMARY KEY(profile, source)
            );
            CREATE TABLE IF NOT EXISTS observations (
                profile TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
                version TEXT NOT NULL, paper_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY(profile, source, source_id, version)
            );
            CREATE INDEX IF NOT EXISTS observation_paper ON observations(profile, paper_key);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL,
                kind TEXT NOT NULL, paper_key TEXT NOT NULL, payload TEXT NOT NULL, detected_at TEXT NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def checkpoint(self, profile, source):
        row = self.db.execute("SELECT * FROM checkpoints WHERE profile=? AND source=?", (profile, source)).fetchone()
        return dict(row) if row else None

    def start_window(self, profile, source, start, end, cursor):
        with self.db:
            self.db.execute("""INSERT INTO checkpoints(profile,source,window_start,window_end,cursor)
                VALUES(?,?,?,?,?) ON CONFLICT(profile,source) DO UPDATE SET
                window_start=excluded.window_start,window_end=excluded.window_end,cursor=excluded.cursor""",
                            (profile, source, start, end, cursor))

    def save_page(self, profile, source, papers, next_cursor, end, detected_at):
        """Paper changes and page cursor commit together, or neither does."""
        changes = []
        with self.db:
            for paper in papers:
                change = self._observe(profile, paper, detected_at)
                if change:
                    changes.append(change)
            if next_cursor is None:
                self.db.execute("""UPDATE checkpoints SET completed_until=?,window_start=NULL,
                    window_end=NULL,cursor=NULL WHERE profile=? AND source=?""", (end, profile, source))
            else:
                self.db.execute("UPDATE checkpoints SET cursor=? WHERE profile=? AND source=?",
                                (next_cursor, profile, source))
        return changes

    def _observe(self, profile, paper, detected_at):
        payload = json.dumps(paper, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        key = (profile, paper["source"], paper["source_id"], paper["version"])
        old = self.db.execute("""SELECT fingerprint FROM observations
            WHERE profile=? AND source=? AND source_id=? AND version=?""", key).fetchone()
        if old and old["fingerprint"] == fingerprint:
            self.db.execute("""UPDATE observations SET last_seen=?
                WHERE profile=? AND source=? AND source_id=? AND version=?""", (detected_at, *key))
            return None
        if old:
            kind = "metadata_updated"
        else:
            known = self.db.execute("SELECT 1 FROM observations WHERE profile=? AND paper_key=? LIMIT 1",
                                    (profile, paper["paper_key"])).fetchone()
            versions = self.db.execute("SELECT version FROM observations WHERE profile=? AND source=? AND source_id=?",
                                      key[:3]).fetchall()
            numeric = [int(v["version"]) for v in versions if v["version"].isdigit()]
            if versions:
                kind = "historical_version" if paper["version"].isdigit() and numeric and int(paper["version"]) < max(numeric) else "new_version"
            else:
                kind = "source_added" if known else "new"
        self.db.execute("""INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(profile,source,source_id,version) DO UPDATE SET
            paper_key=excluded.paper_key,fingerprint=excluded.fingerprint,payload=excluded.payload,last_seen=excluded.last_seen""",
                        (*key, paper["paper_key"], fingerprint, payload, detected_at, detected_at))
        event_id = self.db.execute("INSERT INTO events(profile,kind,paper_key,payload,detected_at) VALUES(?,?,?,?,?)",
                                  (profile, kind, paper["paper_key"], payload, detected_at)).lastrowid
        return {"event_id": event_id, "kind": kind, "paper": paper, "detected_at": detected_at}

    def events(self, profile, after_id=0, limit=100):
        rows = self.db.execute("""SELECT * FROM events WHERE profile=? AND id>? ORDER BY id LIMIT ?""",
                               (profile, after_id, limit)).fetchall()
        return [{"event_id": r["id"], "kind": r["kind"], "paper": json.loads(r["payload"]),
                 "detected_at": r["detected_at"]} for r in rows]
