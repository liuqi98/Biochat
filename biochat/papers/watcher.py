"""Scan independently checkpointed sources and persist an update inbox."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from biochat.papers.config import WatchProfile
from biochat.papers.sources import SOURCES, SourceError, http_session
from biochat.papers.store import PaperStore


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "biochat/paper_monitor"


def _profile(config_path):
    return WatchProfile.from_file(config_path) if config_path else WatchProfile()


def scan_papers(*, state_dir=None, config_path=None, sources=("europepmc", "biorxiv"),
                lookback_days=7, overlap_days=2, max_pages=20, session=None, today=None,
                on_progress=None) -> dict:
    """One bounded scan; incomplete windows resume on the next invocation.

    A separate SQLite write lock serializes scans across processes without
    holding the inbox database open in a long write transaction during HTTP.
    The UTC end date is pinned until each source's window is exhausted.
    """
    profile = _profile(config_path)
    if not sources or any(source not in SOURCES for source in sources) or len(set(sources)) != len(sources):
        raise ValueError(f"Choose distinct sources from {tuple(SOURCES)}")
    for value, low, high, name in ((lookback_days, 1, 365, "lookback_days"),
                                  (overlap_days, 1, 30, "overlap_days"), (max_pages, 1, 1000, "max_pages")):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    today = today or datetime.now(UTC).date()
    root = Path(state_dir) if state_dir else default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = sqlite3.connect(root / "scan_lock.sqlite3", timeout=0)
    try:
        lock.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        lock.close()
        if "locked" in str(exc).lower():
            return {"status": "busy", "message": "Another paper scan is running", "profile": profile.name}
        raise
    store = None
    owned_session = session is None
    changes = []
    report = {"scan_id": uuid.uuid4().hex, "profile": profile.name, "profile_key": profile.key,
              "sources": {}, "state_dir": str(root.resolve()), "status": "complete"}
    try:
        store = PaperStore(root / "papers.sqlite3")
        session = session or http_session()
        for name in sources:
            adapter = SOURCES[name]
            checkpoint = store.checkpoint(profile.key, name)
            if checkpoint and checkpoint["window_start"]:
                start, end, cursor = (checkpoint[k] for k in ("window_start", "window_end", "cursor"))
            else:
                start_date = (date.fromisoformat(checkpoint["completed_until"]) - timedelta(days=overlap_days)
                              if checkpoint and checkpoint["completed_until"] else today - timedelta(days=lookback_days - 1))
                start, end, cursor = min(start_date, today).isoformat(), today.isoformat(), adapter.first_cursor
                store.start_window(profile.key, name, start, end, cursor)
            state = {"window_start": start, "window_end": end, "pages": 0, "records_scanned": 0,
                     "matched_records": 0, "status": "incomplete"}
            report["sources"][name] = state
            seen_cursors = set()
            try:
                for _ in range(max_pages):
                    if on_progress:
                        on_progress({"source": name, "page": state["pages"] + 1, "window_start": start, "window_end": end})
                    seen_cursors.add(cursor)
                    page = adapter.fetch(session, profile, start, end, cursor)
                    if page.next_cursor is not None and page.next_cursor in seen_cursors:
                        raise SourceError("Pagination cursor repeated; checkpoint not advanced")
                    now = datetime.now(UTC).isoformat()
                    changes.extend(store.save_page(profile.key, name, page.papers, page.next_cursor, end, now))
                    state["pages"] += 1
                    state["records_scanned"] += page.records_scanned
                    state["matched_records"] += len(page.papers)
                    if page.next_cursor is None:
                        state["status"] = "complete"
                        break
                    cursor = page.next_cursor
            except SourceError as exc:
                state.update(status="error", error=str(exc))
            state["checkpoint"] = store.checkpoint(profile.key, name)
        if any(s["status"] != "complete" for s in report["sources"].values()):
            report["status"] = "partial"
        report["caught_up"] = all(
            s["status"] == "complete" and s["checkpoint"]["completed_until"] >= today.isoformat()
            for s in report["sources"].values()
        )
        if not report["caught_up"]:
            report["status"] = "partial"
        report["counts"] = dict(Counter(change["kind"] for change in changes))
        report["event_ids"] = [c["event_id"] for c in changes]
        report["changes"] = changes
        report["finished_at"] = datetime.now(UTC).isoformat()
        # Unique immutable run reports; a report write failure cannot erase the inbox.
        folder = root / "runs"
        folder.mkdir(exist_ok=True)
        report_path = folder / f"{report['scan_id']}.json"
        report["report_path"] = str(report_path.resolve())
        temp = report_path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(report_path)
        return report
    finally:
        if store:
            store.close()
        if owned_session and session is not None:
            session.close()
        lock.rollback()
        lock.close()


def read_updates(*, state_dir=None, config_path=None, after_id=0, limit=50) -> dict:
    """Read persisted events by monotonic ID; does not contact providers."""
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("after_id must be nonnegative and limit must be 1–1000")
    profile = _profile(config_path)
    root = Path(state_dir) if state_dir else default_state_dir()
    path = root / "papers.sqlite3"
    if not path.exists():
        return {"profile": profile.name, "events": [], "next_after_id": after_id}
    store = PaperStore(path)
    try:
        events = store.events(profile.key, after_id, limit)
        return {"profile": profile.name, "events": events,
                "next_after_id": events[-1]["event_id"] if events else after_id}
    finally:
        store.close()
