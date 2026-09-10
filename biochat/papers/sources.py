"""Official API adapters; fixed endpoints, bounded requests and resumable pages."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from biochat.papers.config import WatchProfile


class SourceError(RuntimeError):
    """API failure or unexpected payload; callers must not advance the checkpoint."""


@dataclass
class Page:
    papers: list[dict]
    next_cursor: str | None
    records_scanned: int


def http_session() -> requests.Session:
    session = requests.Session()
    # Retry only transient HTTP errors, never arbitrary paper-provided URLs.
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=False)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "Biochat-PaperMonitor/1.0"
    return session


def get_json(session, url, params=None) -> dict:
    try:
        response = session.get(url, params=params, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SourceError(f"{urlparse(url).netloc}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise SourceError("Expected a JSON object")
    if data.get("errMsg") or data.get("error"):
        raise SourceError(f"{urlparse(url).netloc}: API returned an error payload")
    return data


def plain_text(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()


def normalize_doi(value) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def code_links(text: str) -> list[str]:
    links = []
    for url in re.findall(r"https?://[^\s<>\"']+", html.unescape(text)):
        url = url.rstrip(".,;:)]}")
        if urlparse(url).hostname in ("github.com", "gitlab.com", "huggingface.co"):
            links.append(url)
    return sorted(set(links))


def matches_topics(paper: dict, profile: WatchProfile) -> bool:
    text = paper["title"] + " " + paper["abstract"]
    return all(any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I) for term in group)
               for group in profile.topic_groups)


def _paper(source, source_id, doi, title, abstract, **kwargs):
    if not source_id or not plain_text(title):
        raise SourceError(f"{source}: record missing identity or title")
    doi = normalize_doi(doi)
    return {
        "source": source, "source_id": str(source_id),
        "paper_key": "doi:" + doi if doi else f"{source}:{source_id}", "doi": doi,
        "title": plain_text(title), "abstract": plain_text(abstract),
        "code_links": code_links(str(abstract or "")),
        "availability": "discovered_not_validated", **kwargs,
    }


class EuropePMC:
    name = "europepmc"
    first_cursor = "*"

    def fetch(self, session, profile, start, end, cursor) -> Page:
        def quoted(term):
            return 'TITLE_ABS:"' + term.replace("\\", "\\\\").replace('"', '\\"') + '"'
        topic = " AND ".join("(" + " OR ".join(quoted(t) for t in group) + ")" for group in profile.topic_groups)
        query = f"({topic}) AND UPDATE_DATE:[{start} TO {end}]"
        data = get_json(session, "https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
            "query": query, "format": "json", "resultType": "core", "pageSize": 100, "cursorMark": cursor,
        })
        try:
            records = data["resultList"]["result"]
            total = int(data["hitCount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError("Europe PMC search response is incomplete") from exc
        if not isinstance(records, list):
            raise SourceError("Europe PMC records must be an array")
        next_cursor = data.get("nextCursorMark")
        if cursor == self.first_cursor and len(records) == total:
            # The API may omit cursor metadata when all hits fit on page one.
            next_cursor = None
        elif records and (not isinstance(next_cursor, str) or not next_cursor):
            raise SourceError("Europe PMC non-empty page has no cursor")
        # Solr uses an unchanged cursor for exhaustion, including a final empty page.
        next_cursor = None if not records or next_cursor == cursor else next_cursor
        papers = []
        for r in records:
            if not isinstance(r, dict) or not r.get("id") or not r.get("source"):
                raise SourceError("Europe PMC record missing source/id")
            pub_types = r.get("pubTypeList") or {}
            if not isinstance(pub_types, dict) or not isinstance(pub_types.get("pubType", []), list):
                raise SourceError("Europe PMC publication types are malformed")
            paper = _paper(
                self.name, f"{r['source']}:{r['id']}", r.get("doi"), r.get("title"), r.get("abstractText"),
                version=str(r.get("versionNumber") or "metadata"),
                authors=plain_text(r.get("authorString")),
                published_date=r.get("firstPublicationDate", ""),
                record_updated=r.get("dateOfRevision", ""),
                url=f"https://europepmc.org/article/{r['source']}/{r['id']}",
                publication_type=pub_types.get("pubType", []),
                is_open_access=r.get("isOpenAccess") == "Y", published_doi="",
            )
            if matches_topics(paper, profile):
                papers.append(paper)
        return Page(papers, next_cursor, len(records))


class BioRxiv:
    name = "biorxiv"
    first_cursor = "0"

    def fetch(self, session, profile, start, end, cursor) -> Page:
        data = get_json(session, f"https://api.biorxiv.org/details/biorxiv/{start}/{end}/{int(cursor)}/json")
        try:
            message = data["messages"][0]
            status = message["status"]
            records = data["collection"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SourceError("bioRxiv response is incomplete") from exc
        if not isinstance(records, list):
            raise SourceError("bioRxiv records must be an array")
        if status == "no papers found" and not records:
            return Page([], None, 0)
        if status != "ok":
            raise SourceError(f"bioRxiv returned status: {status}")
        try:
            total = int(message["total"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError("bioRxiv pagination total is missing") from exc
        if not records and int(cursor) < total:
            raise SourceError("bioRxiv returned an empty page before exhaustion")
        offset = int(cursor) + len(records)
        papers = []
        for r in records:
            if not isinstance(r, dict) or not normalize_doi(r.get("doi")):
                raise SourceError("bioRxiv record has no valid DOI")
            version = str(r.get("version", ""))
            if not version.isdigit():
                raise SourceError("bioRxiv record has no numeric version")
            paper = _paper(
                self.name, normalize_doi(r["doi"]), r["doi"], r.get("title"), r.get("abstract"),
                version=version, published_date=r.get("date", ""), record_updated=r.get("date", ""),
                authors=plain_text(r.get("authors")),
                url=f"https://www.biorxiv.org/content/{normalize_doi(r['doi'])}v{version}",
                publication_type=["preprint"], is_open_access=None,
                published_doi=normalize_doi(r.get("published")), license=r.get("license", ""),
            )
            if matches_topics(paper, profile):
                papers.append(paper)
        return Page(papers, str(offset) if offset < total else None, len(records))


SOURCES = {source.name: source for source in (EuropePMC(), BioRxiv())}
