"""Offline tests exercise API parsing, durable checkpoints and change identity."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest
import requests

from biochat.papers.config import WatchProfile
from biochat.papers.sources import BioRxiv, EuropePMC, Page, SourceError, _paper, code_links, get_json
from biochat.papers.watcher import read_updates, scan_papers


class FakeSession:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = next(self.payloads)
        if isinstance(payload, Exception):
            raise payload
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return payload
        return Response()


def record(doi="10.1234/example", version="1", source="biorxiv", title="Antibody diffusion design"):
    return _paper(source, doi, doi, title, "A generative nanobody method.", version=version,
                  url="https://example.org/paper", published_date="2026-09-01")


class ScriptedSource:
    first_cursor = "0"

    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def fetch(self, session, profile, start, end, cursor):
        self.calls.append((start, end, cursor))
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page


def install(monkeypatch, pages, name="biorxiv"):
    adapter = ScriptedSource(pages)
    monkeypatch.setitem(__import__("biochat.papers.watcher", fromlist=["SOURCES"]).SOURCES, name, adapter)
    return adapter


def scan(tmp_path, **kwargs):
    return scan_papers(state_dir=tmp_path, sources=("biorxiv",), session=object(),
                       today=date(2026, 9, 9), **kwargs)


def test_repeat_scan_and_overlap_do_not_report_new_papers_again(tmp_path, monkeypatch):
    source = install(monkeypatch, [Page([record()], None, 1), Page([record()], None, 1)])
    first = scan(tmp_path)
    second = scan(tmp_path)
    assert first["counts"] == {"new": 1}
    assert second["counts"] == {}
    assert source.calls == [("2026-09-03", "2026-09-09", "0"), ("2026-09-07", "2026-09-09", "0")]
    assert len(read_updates(state_dir=tmp_path)["events"]) == 1


def test_budget_resume_keeps_window_and_cursor(tmp_path, monkeypatch):
    source = install(monkeypatch, [Page([record()], "30", 30), Page([record("10.1234/next")], None, 1)])
    first = scan(tmp_path, max_pages=1)
    checkpoint = first["sources"]["biorxiv"]["checkpoint"]
    assert first["status"] == "partial"
    assert checkpoint["completed_until"] is None
    assert checkpoint["cursor"] == "30"
    second = scan_papers(state_dir=tmp_path, sources=("biorxiv",), session=object(), today=date(2026, 9, 12))
    assert source.calls[-1] == ("2026-09-03", "2026-09-09", "30")
    assert second["status"] == "partial"  # old window complete, but newer dates still need a scan
    assert second["caught_up"] is False
    assert second["sources"]["biorxiv"]["checkpoint"]["completed_until"] == "2026-09-09"


def test_partial_failure_keeps_committed_pages_and_other_source_continues(tmp_path, monkeypatch):
    install(monkeypatch, [Page([record()], "30", 30), SourceError("temporarily unavailable")])
    install(monkeypatch, [Page([record(source="europepmc")], None, 1)], "europepmc")
    result = scan_papers(state_dir=tmp_path, sources=("biorxiv", "europepmc"), session=object())
    assert result["status"] == "partial"
    assert result["sources"]["biorxiv"]["checkpoint"]["cursor"] == "30"
    assert result["sources"]["europepmc"]["status"] == "complete"
    assert result["counts"] == {"new": 1, "source_added": 1}


def test_versions_and_metadata_changes_are_distinct(tmp_path, monkeypatch):
    install(monkeypatch, [Page([record(version="2")], None, 1), Page([record(version="3")], None, 1),
                         Page([record(version="1")], None, 1),
                         Page([record(version="3", title="Antibody diffusion revised title")], None, 1)])
    assert scan(tmp_path)["counts"] == {"new": 1}
    assert scan(tmp_path)["counts"] == {"new_version": 1}
    assert scan(tmp_path)["counts"] == {"historical_version": 1}
    assert scan(tmp_path)["counts"] == {"metadata_updated": 1}
    page = read_updates(state_dir=tmp_path, limit=2)
    assert [e["kind"] for e in page["events"]] == ["new", "new_version"]
    assert len(read_updates(state_dir=tmp_path, after_id=page["next_after_id"])["events"]) == 2


def test_profile_changes_get_independent_checkpoints(tmp_path, monkeypatch):
    install(monkeypatch, [Page([record()], None, 1), Page([record()], None, 1)])
    scan(tmp_path)
    config = tmp_path / "custom.json"
    config.write_text(json.dumps({"name": "another", "topic_groups": [["nanobody"], ["diffusion"]]}))
    result = scan(tmp_path, config_path=config)
    assert result["counts"] == {"new": 1}
    assert len(read_updates(state_dir=tmp_path, config_path=config)["events"]) == 1


def test_repeated_cursor_is_not_committed(tmp_path, monkeypatch):
    install(monkeypatch, [Page([record()], "0", 1)])
    result = scan(tmp_path)
    assert result["sources"]["biorxiv"]["status"] == "error"
    assert result["counts"] == {}
    assert read_updates(state_dir=tmp_path)["events"] == []


def test_sqlite_scan_lock_does_not_block_reading_and_releases(tmp_path, monkeypatch):
    install(monkeypatch, [Page([], None, 0)])
    lock = sqlite3.connect(tmp_path / "scan_lock.sqlite3", timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    try:
        assert scan(tmp_path)["status"] == "busy"
        assert read_updates(state_dir=tmp_path)["events"] == []
    finally:
        lock.rollback()
        lock.close()
    assert scan(tmp_path)["status"] == "complete"


def test_page_and_checkpoint_roll_back_together_on_storage_error(tmp_path, monkeypatch):
    malformed = record("10.1234/broken")
    del malformed["version"]
    install(monkeypatch, [Page([record(), malformed], "30", 2)])
    with pytest.raises(KeyError):
        scan(tmp_path)
    assert read_updates(state_dir=tmp_path)["events"] == []
    with sqlite3.connect(tmp_path / "papers.sqlite3") as db:
        assert db.execute("SELECT cursor FROM checkpoints").fetchone()[0] == "0"


def test_europepmc_adapter_cursor_query_and_metadata():
    raw = {"id": "123", "source": "MED", "doi": "HTTPS://DOI.ORG/10.1234/Test",
           "title": "<i>Antibody</i> diffusion", "abstractText": 'Model at <a href="https://github.com/example/model">code</a>',
           "firstPublicationDate": "2026-08-01", "dateOfRevision": "2026-09-01", "citedByCount": 999}
    session = FakeSession([{"hitCount": 2, "nextCursorMark": "NEXT", "resultList": {"result": [raw]}},
                           {"hitCount": 2, "nextCursorMark": "NEXT", "resultList": {"result": []}}])
    adapter = EuropePMC()
    page = adapter.fetch(session, WatchProfile(), "2026-09-01", "2026-09-09", "*")
    assert page.next_cursor == "NEXT"
    assert page.papers[0]["paper_key"] == "doi:10.1234/test"
    assert page.papers[0]["code_links"] == ["https://github.com/example/model"]
    assert "citedByCount" not in page.papers[0]  # citation changes are not paper revisions
    assert "UPDATE_DATE:[2026-09-01 TO 2026-09-09]" in session.calls[0][1]["params"]["query"]
    assert adapter.fetch(session, WatchProfile(), "2026-09-01", "2026-09-09", "NEXT").next_cursor is None


def test_biorxiv_filters_locally_but_cursor_counts_all_records():
    rows = [{"doi": "10.1234/one", "title": "Antibody diffusion", "abstract": "", "version": "2", "date": "2026-09-01", "published": "10.1234/journal"},
            {"doi": "10.1234/two", "title": "Unrelated ecology", "abstract": "", "version": "1"}]
    session = FakeSession([{"messages": [{"status": "ok", "total": "3"}], "collection": rows}])
    page = BioRxiv().fetch(session, WatchProfile(), "2026-09-01", "2026-09-09", "0")
    assert page.next_cursor == "2"
    assert page.records_scanned == 2
    assert len(page.papers) == 1
    assert page.papers[0]["published_doi"] == "10.1234/journal"
    assert page.papers[0]["version"] == "2"


def test_europepmc_single_page_can_omit_cursor():
    raw = {"id": "1", "source": "MED", "title": "Antibody diffusion"}
    session = FakeSession([{"hitCount": 1, "resultList": {"result": [raw]}}])
    page = EuropePMC().fetch(session, WatchProfile(), "2026-09-01", "2026-09-09", "*")
    assert page.next_cursor is None
    assert len(page.papers) == 1


def test_europepmc_truncated_page_without_cursor_is_an_error():
    raw = {"id": "1", "source": "MED", "title": "Antibody diffusion"}
    session = FakeSession([{"hitCount": 2, "resultList": {"result": [raw]}}])
    with pytest.raises(SourceError):
        EuropePMC().fetch(session, WatchProfile(), "2026-09-01", "2026-09-09", "*")


@pytest.mark.parametrize("payload", [{}, {"error": "bad query"}, {"messages": [{"status": "unavailable"}], "collection": []}])
def test_api_errors_do_not_look_like_empty_success(payload):
    with pytest.raises(SourceError):
        BioRxiv().fetch(FakeSession([payload]), WatchProfile(), "2026-09-01", "2026-09-09", "0")


def test_biorxiv_empty_window_is_complete():
    payload = {"messages": [{"status": "no papers found"}], "collection": []}
    assert BioRxiv().fetch(FakeSession([payload]), WatchProfile(), "2026-09-01", "2026-09-09", "0").next_cursor is None


def test_network_failures_are_source_errors_and_code_links_are_not_fetched():
    with pytest.raises(SourceError):
        get_json(FakeSession([requests.Timeout()]), "https://api.biorxiv.org/details/biorxiv")
    links = code_links("https://github.com/example/model https://github.com.evil.test/code http://127.0.0.1/private")
    assert links == ["https://github.com/example/model"]


@pytest.mark.parametrize("payload", [
    '{"topic_groups": ["antibody", "diffusion"]}',
    '[]', 'null', '{"topic_groups": null}', '{"topic_groups": 42}',
])
def test_profile_rejects_malformed_groups(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload)
    with pytest.raises(ValueError):
        WatchProfile.from_file(path)


def test_agent_tools_available_in_minimal_and_cli_help():
    from biochat.papers.__main__ import main
    from biochat.tool.paper_monitor import list_paper_updates, monitor_antibody_papers
    from biochat.tool.registry import ToolRegistry
    from biochat.utils.io_utils import load_all_tool_descriptions

    registry = ToolRegistry(load_all_tool_descriptions(profile="minimal"), profile="minimal")
    for tool in (list_paper_updates, monitor_antibody_papers):
        assert registry.get_tool_by_name(tool.__name__)
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
