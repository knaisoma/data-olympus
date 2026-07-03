"""Signal-gated abstention for kb_search (issue #68).

This is the SINGLE source of the abstention gate. Production search (kb_search
``abstain=True``, via ``tools_read.kb_search_fn``) and the benchmark ablation
(``benchmarks/ablate.py``) both import ``SIGNAL_COLUMNS`` and ``abstain_gate``
from here rather than keeping their own copy.

The gate rationale: an out-of-scope query that only lexically overlaps generic
body prose should surface NOTHING rather than a weak, misleading rule. The gate
first runs the query restricted to the discriminating columns (title/tags/
applies_when); if it matches none of them, the query has no real intent signal
and the search abstains (returns no hits). If at least one discriminating column
matches, retrieval proceeds normally over all columns, preserving recall.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_olympus.index import Index, SearchHit

# Discriminating columns for the abstention gate. Deliberately EXCLUDES
# ``description`` and ``body``: their prose carries common words ("governance
# rules for ...") that out-of-scope queries also contain, which would defeat the
# gate. Title, tags, and applies_when are terse and specific, so matching one
# signals real intent. This list is the single definition; the benchmark imports
# it rather than re-declaring it.
SIGNAL_COLUMNS: list[str] = ["title", "tags", "applies_when"]


def has_signal(idx: Index, query: str, *, limit: int, **search_kwargs: object) -> bool:
    """Return True when ``query`` matches at least one discriminating column.

    Runs the query restricted to ``SIGNAL_COLUMNS`` under the same filter kwargs
    (tier/category/status/in_force/doc_type) so the gate is evaluated within the
    same scope the real search will use. An empty/whitespace query has no signal.
    """
    if not query.strip():
        return False
    signal_hits = idx.search(
        query, limit=limit, columns=SIGNAL_COLUMNS, **search_kwargs  # type: ignore[arg-type]
    )
    return bool(signal_hits)


def abstain_gate(
    idx: Index, query: str, *, limit: int, **search_kwargs: object
) -> list[SearchHit] | None:
    """Apply the abstention gate around a normal search.

    Returns ``None`` when the gate FIRES (no discriminating-column signal): the
    caller must treat this as an explicit abstention (empty result with a
    machine-readable reason), NOT as an ordinary zero-hit search. Otherwise runs
    the normal search over all columns (honouring the same filter kwargs) and
    returns its hits (which may itself be empty if nothing ranks).

    ``search_kwargs`` are forwarded to both the signal probe and the real search
    (tier/category/status/in_force/doc_type); ``columns`` must NOT be supplied
    here since the gate controls it for the probe and the real search uses all
    columns.
    """
    if not has_signal(idx, query, limit=limit, **search_kwargs):
        return None
    return idx.search(query, limit=limit, **search_kwargs)  # type: ignore[arg-type]
