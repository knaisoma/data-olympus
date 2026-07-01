"""Synonym / acronym query expansion (issue #38).

Plugs into the ``query_expander`` seam of the search pipeline (issue #36). The
expander rewrites the query term list before the FTS5 MATCH is built. Because
``_build_match_expr`` OR-joins the terms, appending curated synonyms of a term
broadens recall without narrowing it: a ``k8s`` query also reaches documents
that only say ``kubernetes`` (and vice versa).

Design notes:
- The curated map (``DEFAULT_SYNONYMS``) lists one canonical form per group with
  its variants. ``build_synonym_map`` symmetrises it into a lookup where every
  member points at every other member, so expansion is bidirectional.
- Lookup is case-insensitive, but original query terms keep their casing (FTS5
  matching is case-insensitive anyway). Synonyms are appended after the original
  terms so BM25 still favours the terms the user actually typed.
- Expansion is bounded (``max_terms``) and de-duplicated to avoid runaway MATCH
  expressions. Original terms are never dropped by the cap.
- Configuration follows the ``config.py`` env pattern: ``KB_SYNONYMS`` overrides
  or merges the map, ``KB_SYNONYMS_MODE`` selects merge/replace/off.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

# Curated default groups: canonical form -> its acronyms / long forms / variants.
# Keep groups small and unambiguous; a synonym that is a common English word in
# other contexts (e.g. "role") is deliberately avoided to keep recall precise.
DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "kubernetes": ["k8s", "microk8s"],
    "authentication": ["auth", "authn"],
    "authorization": ["authz"],
    "row level security": ["rls"],
    "architecture decision record": ["adr"],
    "continuous integration": ["ci"],
    "continuous delivery": ["cd"],
    "pull request": ["pr", "mr", "merge request"],
    "configuration": ["config"],
    "repository": ["repo"],
    "database": ["db"],
    "documentation": ["docs"],
    "knowledge base": ["kb"],
    "personally identifiable information": ["pii"],
    "single sign on": ["sso"],
    "role based access control": ["rbac"],
    "infrastructure as code": ["iac"],
}

# Upper bound on the expanded term list. Keeps the FTS MATCH expression (and thus
# query latency) bounded regardless of how synonym-dense a query is.
DEFAULT_MAX_TERMS = 32


def build_synonym_map(groups: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Symmetrise curated groups into a bidirectional lookup.

    Each group (canonical key + its variants) becomes a fully-connected set: every
    member maps to all other members of the group. Keys are lower-cased; a
    multi-word member is kept verbatim (lower-cased) so multi-word synonyms are
    possible, though single-token members drive the common case. Duplicate
    members across groups are merged (union of neighbours), order-stable.
    """
    lookup: dict[str, list[str]] = {}
    for canonical, variants in groups.items():
        members = [canonical.lower(), *[v.lower() for v in variants]]
        # De-dup members while preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for m in members:
            if m not in seen:
                ordered.append(m)
                seen.add(m)
        for member in ordered:
            others = [m for m in ordered if m != member]
            bucket = lookup.setdefault(member, [])
            for other in others:
                if other not in bucket:
                    bucket.append(other)
    return lookup


def make_synonym_expander(
    groups: Mapping[str, Iterable[str]] | None = None,
    *,
    max_terms: int = DEFAULT_MAX_TERMS,
    _prebuilt: Mapping[str, list[str]] | None = None,
) -> Callable[[list[str]], list[str]]:
    """Build a ``query_expander`` closure from curated groups.

    ``groups`` is the canonical-form map (like ``DEFAULT_SYNONYMS``); it is
    symmetrised internally. Pass ``_prebuilt`` to supply an already-symmetrised
    lookup (from ``build_synonym_map`` / ``load_synonyms_from_env``) and skip the
    rebuild. The returned callable matches the
    ``Callable[[list[str]], list[str]]`` signature the ``Index`` expects.
    """
    lookup = dict(_prebuilt) if _prebuilt is not None else build_synonym_map(groups or {})

    def expander(terms: list[str]) -> list[str]:
        # Original terms first (order-stable, de-duplicated), then synonyms.
        out: list[str] = []
        seen: set[str] = set()
        for t in terms:
            if t not in seen:
                out.append(t)
                seen.add(t)
        for t in terms:
            for syn in lookup.get(t.lower(), ()):
                if len(out) >= max_terms:
                    return out
                if syn not in seen:
                    out.append(syn)
                    seen.add(syn)
        return out

    return expander


def _parse_synonyms_spec(raw: str) -> dict[str, list[str]]:
    """Parse a ``KB_SYNONYMS`` spec: ``key=a,b;key2=c,d`` (groups ``;``-split).

    Within a group the ``key`` is the canonical form and the comma-separated tail
    are its variants. Whitespace is trimmed; empty entries are dropped. A group
    without ``=`` or without variants is ignored.
    """
    groups: dict[str, list[str]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, tail = chunk.partition("=")
        key = key.strip()
        variants = [v.strip() for v in tail.split(",") if v.strip()]
        if key and variants:
            groups[key] = variants
    return groups


def load_synonyms_from_env() -> dict[str, list[str]]:
    """Load the symmetrised synonym lookup from the environment.

    Env knobs (all optional; sane defaults):
    - ``KB_SYNONYMS_MODE``: ``merge`` (default) layers ``KB_SYNONYMS`` on top of
      the curated defaults; ``replace`` uses only ``KB_SYNONYMS``; ``off``
      disables expansion (returns an empty lookup, so ``make_synonym_expander``
      is a passthrough).
    - ``KB_SYNONYMS``: extra/override groups in ``key=a,b;key2=c`` form.

    Returns the symmetrised lookup (as ``build_synonym_map`` produces).
    """
    mode = os.getenv("KB_SYNONYMS_MODE", "merge").strip().lower()
    if mode == "off":
        return {}
    extra = _parse_synonyms_spec(os.getenv("KB_SYNONYMS", ""))
    if mode == "replace":
        groups: dict[str, list[str]] = dict(extra)
    else:  # merge (default)
        groups = {k: list(v) for k, v in DEFAULT_SYNONYMS.items()}
        groups.update(extra)
    return build_synonym_map(groups)


def default_query_expander() -> Callable[[list[str]], list[str]]:
    """The serving expander: env-configured, defaulting to the curated map."""
    return make_synonym_expander(_prebuilt=load_synonyms_from_env())
