"""Corpus co-occurrence query expansion (issue #40).

Embedding-free semantic broadening. At index-build time we learn, for each
reasonable term in the corpus, the handful of other terms it most strongly
co-occurs with (measured by pointwise mutual information, PMI, at document
granularity) and store a small bounded ``related_terms`` table alongside the
FTS index. At query time an expander looks each query term up in that table and
appends its top related terms. Those appended terms are DOWN-WEIGHTED not by
their position (FTS5 bm25 has no positional preference) but because
``Index.search`` matches the expansion terms in a SEPARATE penalized backfill
pass whose hits can only rank BELOW the worst primary-term hit (finding (a); see
``Index._expansion_backfill``). So a doc that matches only a corpus-related term
can never outrank a doc matching a term the user actually typed.

Why PMI over raw co-occurrence counts: raw counts are dominated by globally
frequent tokens (every doc mentions "the", "a", "you"), so a raw-count table
would relate every term to the same stopword-ish set. PMI normalises by the
marginal frequencies, surfacing terms that co-occur *more than chance* -- the
signal we want. Stopword-like and very short tokens are dropped up front so
they neither pollute the table nor consume a related-term slot.

Design constraints (see issue #40):
- The table is built as part of ``Index.build`` into the same tmp DB and swapped
  atomically, so a query never sees a half-built table.
- Hard-bounded: top-k related terms per term (``k`` small, default 5) above a
  minimum co-occurrence count and PMI threshold; only "reasonable" tokens (min
  length, not a stopword, alphabetic-ish) are considered as heads or tails.
- Build cost stays negligible at the current corpus scale: a single pass to
  build per-doc token sets, then pair counting restricted to the kept
  vocabulary.
- Composes WITH the synonym expander (issue #38) rather than replacing it; see
  ``compose_expanders``.
- Configuration follows ``config.py``: ``KB_COOCCURRENCE_MODE`` (on/off),
  ``KB_COOCCURRENCE_K``, ``KB_COOCCURRENCE_MIN_PMI``, ``KB_COOCCURRENCE_MIN_COUNT``.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable

# Token = a run of letters (with internal digits allowed after the first letter,
# so "k8s"/"utf8" survive) of at least MIN_TOKEN_LEN characters. Pure numbers and
# punctuation-only runs are excluded. Matching is case-insensitive; tokens are
# lower-cased.
_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")

# Minimum token length to be a co-occurrence head or tail. Short tokens ("is",
# "to", "db") carry little topical signal and already match literally without
# expansion, so they are skipped to keep the table focused.
MIN_TOKEN_LEN = 3

# A compact, deployment-neutral English stopword set plus a few markdown/tech
# filler words. Kept intentionally small: PMI already suppresses ubiquitous
# tokens, this list just avoids wasting related-term slots on obvious noise.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
    "man", "new", "now", "old", "see", "two", "way", "who", "did", "its", "let",
    "put", "say", "she", "too", "use", "this", "that", "with", "from", "they",
    "will", "would", "there", "their", "what", "which", "when", "where", "while",
    "about", "into", "your", "only", "other", "than", "then", "them", "these",
    "those", "been", "being", "have", "also", "such", "very", "much", "more",
    "most", "some", "many", "each", "both", "few", "own", "same", "over", "under",
    "above", "below", "after", "before", "between", "because", "during",
    "through", "against", "doc", "docs", "note", "notes", "via", "etc", "within",
    "without", "upon", "per",
})


def is_reasonable_token(token: str) -> bool:
    """True if ``token`` is a plausible co-occurrence head/tail.

    Reasonable = at least ``MIN_TOKEN_LEN`` chars, matches the token shape (a
    letter then letters/digits), and is not a stopword. The token is assumed to
    already be lower-cased.
    """
    if len(token) < MIN_TOKEN_LEN:
        return False
    if token in _STOPWORDS:
        return False
    return bool(_TOKEN_RE.fullmatch(token))


def tokenize_doc(*parts: str) -> set[str]:
    """Return the SET of reasonable tokens in the given text parts.

    A set (not a bag) because co-occurrence is measured at document granularity:
    whether a term appears in a document, not how many times. Deduping per-doc
    keeps a term that is repeated many times in one doc from dominating counts.
    """
    tokens: set[str] = set()
    for part in parts:
        for m in _TOKEN_RE.finditer(part.lower()):
            tok = m.group(0)
            if tok not in _STOPWORDS:
                tokens.add(tok)
    return tokens


# Defaults for the bounded table. Kept small so both build cost and query-time
# MATCH growth stay negligible.
#
# min_count=3 / min_pmi>0 (finding (b)): at min_count=2 / min_pmi=0 a single pair
# of docs sharing any two tokens produces a "related" edge, which on a small
# corpus is noise, not signal. Requiring a pair to co-occur in at least 3 docs
# and to have STRICTLY positive PMI (co-occur MORE than chance) keeps only edges
# with real corpus support.
DEFAULT_K = 5
DEFAULT_MIN_COUNT = 3
DEFAULT_MIN_PMI = 0.1

# Corpus-size floor below which co-occurrence expansion is auto-disabled (finding
# (b)). PMI is a ratio estimate; on a handful of docs the marginal counts are too
# small for it to mean anything, so we skip building the table entirely rather
# than emit noise. A deployment can lower it via KB_COOCCURRENCE_MIN_DOCS.
DEFAULT_MIN_DOCS = 50

# Cap on the number of unique tokens from one document fed into pair counting
# (finding (b)). Pair counting is O(unique-tokens^2) per doc; a multi-thousand-
# word doc with thousands of unique tokens is a build-time memory/CPU cliff. The
# most topical tokens are kept (longest first, then alphabetical for determinism)
# so the cap trims the long tail of incidental vocabulary, not the signal.
DEFAULT_MAX_DOC_TOKENS = 400


def build_cooccurrence_table(
    doc_token_sets: Iterable[set[str]],
    *,
    k: int = DEFAULT_K,
    min_count: int = DEFAULT_MIN_COUNT,
    min_pmi: float = DEFAULT_MIN_PMI,
    min_docs: int = DEFAULT_MIN_DOCS,
    max_doc_tokens: int = DEFAULT_MAX_DOC_TOKENS,
) -> dict[str, list[str]]:
    """Compute a bounded ``{term: [related...]}`` table from per-doc token sets.

    For each unordered pair (a, b) co-occurring in at least ``min_count``
    documents, the pointwise mutual information is::

        pmi(a, b) = log( P(a, b) / (P(a) * P(b)) )
                  = log( (c_ab * N) / (c_a * c_b) )

    where ``N`` is the document count, ``c_a``/``c_b`` the per-term document
    counts and ``c_ab`` the co-occurrence count. Pairs with ``pmi < min_pmi``
    are dropped. For each term the top-``k`` partners by PMI (ties broken by
    co-occurrence count, then alphabetically for determinism) are kept.

    The result is directional in storage (both ``a -> b`` and ``b -> a`` are
    emitted, each capped independently at k) so a lookup of either term finds the
    other. Returns an order-stable dict; each value is at most ``k`` long.

    Returns the empty table (co-occurrence auto-disabled) when the corpus has
    fewer than ``min_docs`` documents (finding (b)): PMI is a ratio estimate that
    is pure noise on a handful of docs. Each document contributes at most
    ``max_doc_tokens`` unique tokens to pair counting (finding (b)); pair counting
    is O(unique-tokens^2) per doc, so an unbounded huge doc is a build-time cliff.
    The kept tokens are the longest ones (most topical), tie-broken alphabetically.
    """
    materialised = [s for s in doc_token_sets if s]
    n_docs = len(materialised)
    # Corpus-size floor: below it PMI is noise, so skip building entirely.
    if n_docs < max(2, min_docs) or k <= 0:
        return {}

    term_counts: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    for tokens in materialised:
        ordered = sorted(tokens)
        # Cap the per-doc unique-token count fed into O(n^2) pair counting. Keep
        # the longest tokens (most topical), tie-broken alphabetically so the
        # trim is deterministic, then re-sort alphabetically for stable pairing.
        if max_doc_tokens > 0 and len(ordered) > max_doc_tokens:
            ordered = sorted(
                sorted(ordered, key=lambda t: (-len(t), t))[:max_doc_tokens]
            )
        for tok in ordered:
            term_counts[tok] += 1
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                pair_counts[(a, b)] += 1

    # candidates[term] = list of (pmi, count, other)
    candidates: dict[str, list[tuple[float, int, str]]] = {}
    for (a, b), c_ab in pair_counts.items():
        if c_ab < min_count:
            continue
        c_a = term_counts[a]
        c_b = term_counts[b]
        pmi = math.log((c_ab * n_docs) / (c_a * c_b))
        if pmi < min_pmi:
            continue
        candidates.setdefault(a, []).append((pmi, c_ab, b))
        candidates.setdefault(b, []).append((pmi, c_ab, a))

    table: dict[str, list[str]] = {}
    for term, partners in candidates.items():
        # Sort by PMI desc, then count desc, then partner asc (deterministic).
        partners.sort(key=lambda t: (-t[0], -t[1], t[2]))
        table[term] = [other for _pmi, _c, other in partners[:k]]
    return table


# --- SQLite persistence (built inside Index.build, read at query time) --------

RELATED_TERMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS related_terms (
    term TEXT NOT NULL,
    related TEXT NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (term, related)
);
CREATE INDEX IF NOT EXISTS idx_related_terms_term ON related_terms (term);
"""


def write_cooccurrence_table(
    conn: sqlite3.Connection, table: dict[str, list[str]],
) -> None:
    """Populate the ``related_terms`` table on an open (tmp-build) connection.

    Called from ``Index.build`` against the tmp DB before the atomic swap, so
    the table is part of the same build and never seen half-populated. ``rank``
    (0-based) preserves the PMI ordering so the query-time lookup can keep the
    strongest partners first.
    """
    rows = [
        (term, related, rank)
        for term, partners in table.items()
        for rank, related in enumerate(partners)
    ]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO related_terms (term, related, rank) "
            "VALUES (?, ?, ?)",
            rows,
        )


def lookup_related_terms(
    conn: sqlite3.Connection, term: str, *, limit: int,
) -> list[str]:
    """Return up to ``limit`` related terms for ``term``, strongest first.

    Reads the ``related_terms`` table; ordered by the stored ``rank`` so the
    highest-PMI partners come first. An unknown term (or a build with no table)
    yields the empty list.
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT related FROM related_terms WHERE term = ? ORDER BY rank LIMIT ?",
        (term.lower(), limit),
    ).fetchall()
    return [r[0] for r in rows]


# --- query-time expander ------------------------------------------------------

# Upper bound on the expanded term list, matching the synonym expander's cap so a
# composed chain stays bounded regardless of how many related terms exist.
DEFAULT_MAX_TERMS = 32


def make_cooccurrence_expander(
    lookup: Callable[[str, int], list[str]],
    *,
    k: int = DEFAULT_K,
    max_terms: int = DEFAULT_MAX_TERMS,
) -> Callable[[list[str]], list[str]]:
    """Build a ``query_expander`` closure backed by a related-terms lookup.

    ``lookup(term, k)`` returns the related terms for ``term`` (typically bound
    to the live index DB via ``Index``). The returned callable keeps the original
    terms first (order-stable, de-duplicated) and then appends related terms
    (also de-duplicated, bounded by ``max_terms``). Originals are never dropped
    by the cap. The appended related terms are down-weighted by ``Index.search``
    matching them in a separate penalized backfill pass (they can only rank below
    the primary hits); the "first" positioning here is only so ``Index.search``
    can split originals from expansion terms, NOT a bm25 positional effect.
    """

    def expander(terms: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for t in terms:
            low = t.lower()
            if low not in seen:
                out.append(t)
                seen.add(low)
        # Append related terms for each ORIGINAL term (not the appended ones, to
        # avoid a second-order expansion blow-up).
        for t in terms:
            if len(out) >= max_terms:
                break
            for related in lookup(t.lower(), k):
                if len(out) >= max_terms:
                    break
                if related not in seen:
                    out.append(related)
                    seen.add(related)
        return out

    return expander


# --- composition --------------------------------------------------------------


def compose_expanders(
    *expanders: Callable[[list[str]], list[str]] | None,
    max_terms: int = DEFAULT_MAX_TERMS,
) -> Callable[[list[str]], list[str]] | None:
    """Chain query expanders left-to-right, feeding each output into the next.

    ``None`` expanders are skipped. Returns ``None`` when nothing is left (so the
    caller can treat "no expansion" uniformly). The composed output is
    de-duplicated (case-insensitively, order-stable) and bounded by ``max_terms``
    as a final safety net, so composing two bounded expanders can never exceed
    the cap. Order matters: pass the synonym expander first, then the
    co-occurrence expander, so co-occurrence broadens the synonym-expanded set.
    """
    active = [e for e in expanders if e is not None]
    if not active:
        return None

    def composed(terms: list[str]) -> list[str]:
        current = terms
        for expander in active:
            current = expander(current)
        # Final de-dup + cap (case-insensitive, order-stable).
        out: list[str] = []
        seen: set[str] = set()
        for t in current:
            low = t.lower()
            if low not in seen:
                out.append(t)
                seen.add(low)
            if len(out) >= max_terms:
                break
        return out

    return composed


# --- env configuration --------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


def cooccurrence_enabled() -> bool:
    """Whether co-occurrence expansion is active. Default ON.

    ``KB_COOCCURRENCE_MODE=off`` disables it (both the build-time table and the
    query-time expander); any other value (including unset) leaves it on.
    """
    return os.getenv("KB_COOCCURRENCE_MODE", "on").strip().lower() != "off"


def cooccurrence_build_params() -> dict[str, int | float]:
    """Build-time knobs from env: k, min_count, min_pmi, min_docs, max_doc_tokens.

    All have sane defaults (see the module constants). ``min_docs`` is the corpus
    floor below which the table is auto-disabled; ``max_doc_tokens`` caps the
    per-doc unique tokens fed into O(n^2) pair counting (finding (b)).
    """
    return {
        "k": _env_int("KB_COOCCURRENCE_K", DEFAULT_K),
        "min_count": _env_int("KB_COOCCURRENCE_MIN_COUNT", DEFAULT_MIN_COUNT),
        "min_pmi": _env_float("KB_COOCCURRENCE_MIN_PMI", DEFAULT_MIN_PMI),
        "min_docs": _env_int("KB_COOCCURRENCE_MIN_DOCS", DEFAULT_MIN_DOCS),
        "max_doc_tokens": _env_int(
            "KB_COOCCURRENCE_MAX_DOC_TOKENS", DEFAULT_MAX_DOC_TOKENS
        ),
    }
