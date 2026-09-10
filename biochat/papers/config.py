"""A query profile: AND between topic groups, OR within each group."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_GROUPS = (
    ("antibody", "antibodies", "nanobody", "nanobodies", "VHH", "CDRH3", "HCDR3", "single-domain antibody"),
    ("machine learning", "deep learning", "diffusion", "generative", "language model", "inverse folding",
     "de novo design", "artificial intelligence", "ProteinMPNN", "RFdiffusion", "RFantibody"),
)


@dataclass(frozen=True)
class WatchProfile:
    name: str = "ai_antibody"
    topic_groups: tuple[tuple[str, ...], ...] = DEFAULT_GROUPS

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Profile name must be non-empty")
        if not isinstance(self.topic_groups, (list, tuple)) or not self.topic_groups or len(self.topic_groups) > 10:
            raise ValueError("Provide 1–10 topic groups")
        for group in self.topic_groups:
            if not isinstance(group, (list, tuple)) or not group or len(group) > 50:
                raise ValueError("Each topic group needs 1–50 terms")
            if any(not isinstance(term, str) or not term.strip() or len(term) > 120 for term in group):
                raise ValueError("Terms must be non-empty strings of at most 120 characters")

    @property
    def key(self) -> str:
        data = {"name": self.name, "groups": self.topic_groups, "query_version": 1}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_file(cls, path: str | Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Profile must be a JSON object")
        groups = data.get("topic_groups", DEFAULT_GROUPS)
        if not isinstance(groups, list):
            # Default groups are tuples; custom JSON must use arrays.
            if "topic_groups" in data:
                raise ValueError("topic_groups must be an array of arrays")
        profile = cls(name=data.get("name", "custom"), topic_groups=groups)
        return cls(profile.name, tuple(tuple(term.strip() for term in group) for group in profile.topic_groups))
