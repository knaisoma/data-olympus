"""data-olympus Index retrieval method.

Uses the real data_olympus.index.Index: outline() for a cheap structural map,
search() with in_force=True (the in-force status class active/accepted/approved,
excluding superseded/deprecated), then get() on the top hit. This is the
selective-loading method being benchmarked.

Previously this used ``status="active"``, which silently excluded ``accepted``
gold decision docs (benchmark bug B1) and produced a misleading exact-recall
figure. The production ``in_force`` filter is the deployable equivalent and
includes accepted/approved decisions, so the harness now measures a mode a real
agent can actually invoke.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.methods.base import RetrievalResult, dedupe

if TYPE_CHECKING:
    from data_olympus.index import Index


def _render_outline(outline: list[dict[str, object]]) -> str:
    """Render the outline into a compact text map."""
    lines: list[str] = []
    for tier in outline:
        cats = tier.get("categories", [])
        assert isinstance(cats, list)
        cat_parts = ", ".join(
            f"{c['name']}({c['count']})" for c in cats  # type: ignore[index]
        )
        lines.append(f"{tier['name']}: {cat_parts}")
    return "\n".join(lines)


class DataOlympusMethod:
    """Retrieval method using data-olympus Index (selective loading)."""

    name = "data-olympus"
    ranks = True  # FTS bm25 order is a real ranking signal

    def __init__(self, idx: Index, limit: int = 5) -> None:
        self._idx = idx
        self._limit = limit

    def retrieve(self, query: str) -> RetrievalResult:
        outline_text = _render_outline(self._idx.outline())
        hits = self._idx.search(query, limit=self._limit, in_force=True)
        ranked = dedupe([h.id for h in hits])
        parts: list[str] = [outline_text]
        parts.extend(f"{h.title}: {h.snippet}" for h in hits)
        if hits:
            top = self._idx.get(hits[0].id)
            if top is not None:
                parts.append(top.content_markdown)
        return RetrievalResult(
            payload_text="\n".join(parts),
            ranked_ids=ranked,
            retrieved_ids=set(ranked),
        )
