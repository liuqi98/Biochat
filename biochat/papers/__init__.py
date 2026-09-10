"""Incremental literature monitoring; no LLM, full-text execution or model install."""

from biochat.papers.watcher import scan_papers, read_updates

__all__ = ["scan_papers", "read_updates"]
