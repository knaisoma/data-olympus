"""Common types for retrieval methods."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalResult:
    payload_text: str
    ranked_ids: list[str]   # best-first, deduped
    retrieved_ids: set[str]


def dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
