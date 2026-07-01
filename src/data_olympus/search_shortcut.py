"""Exact-id and exact-tag short-circuit reranker (issue #39).

When a query is a single token that is an exact document id (``STD-U-002``,
``DEC-001``, or a path-derived id like ``tooling-AGENTS``) or an exact tag, that
document should rank FIRST instead of competing on full-text bm25 score, which
ties or loses when the id/tag also appears verbatim in unrelated documents.

This plugs into the ``reranker`` seam of :class:`data_olympus.index.Index`
(signature ``Callable[[str, list[SearchHit]], list[SearchHit]]``). The reranker
runs AFTER the bm25-ordered ``search()`` results are produced, so:

- Exact id: a single-token query is looked up directly with ``Index.get`` (the
  match stage never guarantees the id doc is in, or at the top of, the FTS hit
  list). If the doc exists it is prepended (or moved to the front) so it is the
  top hit even when it is absent from the FTS results.
- Exact tag: docs in the current hit list that carry the token as an EXACT tag
  are moved ahead of the rest, preserving their relative bm25 order.

Detection is conservative: only a single-token query is considered, and the
token must survive :func:`looks_like_id` / :func:`looks_like_tag` before any
lookup. Ordinary multi-term full-text queries are passed through untouched, and
``_build_match_expr`` is not modified.

The reranker is composable: pass an ``inner`` reranker (e.g. a status/tier
prior) and it runs FIRST; the id/tag short-circuit is then applied on top so the
exact match wins regardless of the inner ordering.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from data_olympus.index import SearchHit

if TYPE_CHECKING:
    from collections.abc import Callable


# A conservative id shape: a single token of alphanumeric segments joined by
# '-', '.', '_' or ':' with at least one separator. Matches KB ids
# (STD-U-002, DEC-001, STD-BN-001, ADR-014) and path-derived ids
# (tooling-AGENTS, projects-example-project-README). Does NOT match ordinary
# single words ("caching", "worktree") or natural-language phrases. Detection
# is only a cheap pre-filter: the authoritative check is Index.get() returning a
# real document.
_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+(?:[-._:][A-Za-z0-9]+)+$")

# A tag is a looser single token (tags like "style", "policy", "backend-nestjs"
# can be a bare word). We still require the query to be a single token, then
# gate on an EXACT tag match in the corpus, so a bare word only short-circuits
# when it is literally a tag on some document.
_TAG_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-._:]*$")


class _IdTagIndex(Protocol):
    """The slice of Index this reranker depends on (keeps it testable)."""

    def get(self, id: str) -> object | None: ...

    def ids_with_exact_tag(self, tag: str) -> set[str]: ...


def looks_like_id(query: str) -> str | None:
    """Return the single id-shaped token, or None if the query is not id-shaped.

    Conservative: the query must be exactly one token and match the id pattern
    (at least one separator between alphanumeric segments).
    """
    token = query.strip()
    if not token or " " in token or "\t" in token:
        return None
    return token if _ID_TOKEN_RE.match(token) else None


def looks_like_tag(query: str) -> str | None:
    """Return the single tag-shaped token, or None.

    Conservative: exactly one token matching the tag pattern. Whether it is
    actually a tag is decided by the exact-tag lookup, not by this shape check.
    """
    token = query.strip()
    if not token or " " in token or "\t" in token:
        return None
    return token if _TAG_TOKEN_RE.match(token) else None


def _hit_from_doc(doc: object) -> SearchHit:
    """Build a synthetic top hit for a directly-fetched document.

    score is set below any real bm25 score (bm25 is <= 0 and lower is better;
    ``-inf`` guarantees the id doc sorts first if a caller re-sorts by score).
    """
    return SearchHit(
        id=getattr(doc, "id", ""),
        path=getattr(doc, "path", ""),
        title=getattr(doc, "title", ""),
        snippet=getattr(doc, "description", "") or "",
        score=float("-inf"),
        status=getattr(doc, "status", "") or "",
        doc_type=getattr(doc, "doc_type", "") or "",
    )


def make_id_tag_reranker(
    index: _IdTagIndex,
    *,
    inner: Callable[[str, list[SearchHit]], list[SearchHit]] | None = None,
) -> Callable[[str, list[SearchHit]], list[SearchHit]]:
    """Build a reranker that short-circuits exact-id and exact-tag queries.

    ``inner`` runs first (compose an existing status/tier reranker here); the
    id/tag short-circuit is then applied so the exact match wins regardless of
    the inner ordering. Both stages default to identity behaviour, so an
    ordinary multi-term query is returned unchanged (aside from ``inner``).
    """

    def reranker(query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if inner is not None:
            hits = list(inner(query, hits))

        # --- exact id: single id-shaped token that resolves to a real doc ---
        id_token = looks_like_id(query)
        if id_token is not None:
            doc = index.get(id_token)
            if doc is not None:
                doc_id = getattr(doc, "id", id_token)
                existing = next((h for h in hits if h.id == doc_id), None)
                rest = [h for h in hits if h.id != doc_id]
                top = existing if existing is not None else _hit_from_doc(doc)
                return [top, *rest]

        # --- exact tag: single token that is literally a tag on some docs ---
        tag_token = looks_like_tag(query)
        if tag_token is not None:
            tagged_ids = index.ids_with_exact_tag(tag_token)
            if tagged_ids:
                tagged = [h for h in hits if h.id in tagged_ids]
                untagged = [h for h in hits if h.id not in tagged_ids]
                if tagged:
                    # Preserve each group's relative (bm25 / inner) order;
                    # only lift the tagged group ahead of the untagged group.
                    return [*tagged, *untagged]

        return hits

    return reranker
