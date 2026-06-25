"""Types describing a generated benchmark corpus."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Concept:
    id: str
    path: str          # bundle-relative
    tier: str
    type: str
    status: str
    title: str
    topic: str
    body: str


@dataclass(frozen=True)
class TopicRecord:
    """Ground truth for one topic: the current concept and optional stale one."""

    topic: str
    current_id: str
    current_type: str
    stale_id: str | None = None


@dataclass(frozen=True)
class CorpusManifest:
    concepts: list[Concept] = field(default_factory=list)
    topics: list[TopicRecord] = field(default_factory=list)
