"""kb_curate (issue #31, first slice): lists in-force documents that are
review-due, most-overdue first.

Advisory and human-gated: this tool RECOMMENDS, it never proposes, edits,
promotes or demotes anything. It takes no write-pipeline dependency at all
(no pending queue, no worktrees, no audit log) -- there is nothing here that
could mutate the corpus even by mistake.

Reuses compute_freshness (issue #142) as the single definition of "review
due", the same one kb_search and kb_get already expose per-hit via
``freshness``/``freshness_reason``. The candidate set (which documents even
count) comes from Index.curate_candidates, which reuses the SAME in-force
predicate kb_search(in_force=True) uses, so this list is exactly "governed
documents review-due", never a second notion of "in force".

Name reserved on issue #31 to avoid colliding with the existing kb_audit
event-log tool.

Pattern promotion -- surfacing repeated patterns across the corpus and
proposing to hoist them up the tier chain via kb_propose_edit -- is the OTHER
half of issue #31 and is explicitly NOT in scope here. It needs its own
detection design and is not a bounded slice.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.models import CurateEntry, CurateResponse

if TYPE_CHECKING:
    from data_olympus.index import Index

# Same rationale as kb_search's clamp: an unbounded scan of a large corpus in
# one response would be an expensive read-tool call, and an operator working
# through a review queue wants a manageable list, not the whole graph.
_DEFAULT_LIMIT = 50


def _overdue_days(
    *,
    recheck_by: str,
    last_verified: str,
    today: str,
    review_due_after_days: int | None,
) -> float:
    """A sortable "how overdue" key for a document ALREADY known to be
    ``stale`` (the caller checks that with :func:`compute_freshness` first).
    Higher is more overdue. Mirrors compute_freshness's own priority
    (an explicit recheck_by overrides the age derivation) without
    duplicating its state logic -- this only orders entries the state
    computation already selected.

    A document with no ``last_verified`` at all has no age to measure and is
    the most urgent case (nobody has ever checked it), so it sorts as
    infinitely overdue -- ahead of any doc that was at least verified once,
    however long ago.
    """
    import datetime

    if recheck_by and recheck_by < today:
        return float(
            (datetime.date.fromisoformat(today)
             - datetime.date.fromisoformat(recheck_by)).days
        )
    if not last_verified:
        return float("inf")
    try:
        age_days = (
            datetime.date.fromisoformat(today)
            - datetime.date.fromisoformat(last_verified)
        ).days
    except ValueError:
        return 0.0
    threshold = review_due_after_days or 0
    return float(age_days - threshold)


def kb_curate_fn(
    *,
    idx: Index,
    today: str | None = None,
    review_due_after_days: int | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> CurateResponse:
    """Which in-force documents are due for review, and why, most-overdue
    first. Returns a valid empty result (never an error) for an empty corpus
    or when nothing is review-due -- including when ``review_due_after_days``
    is unset, which disables the derivation entirely (matches
    compute_freshness's own feature-off default)."""
    from data_olympus.format.validate import compute_freshness, today_iso

    today = today if today is not None else today_iso()
    # Same clamp shape as kb_search_fn (issue #65): bound both ends so a
    # negative limit cannot reach downstream slicing as "no limit".
    if limit > 100:
        limit = 100
    elif limit < 1:
        limit = 1
    candidates = idx.curate_candidates(today=today)
    scored: list[tuple[float, CurateEntry]] = []
    for row in candidates:
        state, reason = compute_freshness(
            valid_from=str(row.get("valid_from") or ""),
            valid_until=str(row.get("valid_until") or ""),
            recheck_by=str(row.get("recheck_by") or ""),
            last_verified=str(row.get("last_verified") or ""),
            today=today,
            review_due_after_days=review_due_after_days,
        )
        if state != "stale":
            continue
        overdue = _overdue_days(
            recheck_by=str(row.get("recheck_by") or ""),
            last_verified=str(row.get("last_verified") or ""),
            today=today,
            review_due_after_days=review_due_after_days,
        )
        scored.append((
            overdue,
            CurateEntry(
                id=str(row["id"]), path=str(row["path"]), title=str(row["title"]),
                reason=reason or "",
            ),
        ))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    entries = [entry for _, entry in scored[:limit]]
    return CurateResponse(entries=entries, total=len(entries))
