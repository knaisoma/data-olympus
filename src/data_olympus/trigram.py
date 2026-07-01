"""Trigram fuzzy-match fallback for typos and partial identifiers (issue #41).

The main FTS index uses the ``porter unicode61`` tokenizer, which matches whole
(stemmed) words. That misses two common query shapes:

- a misspelling one edit off a real term (``observabilty`` for ``observability``)
- a partial / truncated identifier (``kubernet`` for ``kubernetes``)

A second FTS5 virtual table tokenized with ``trigram`` indexes the same columns.
The trigram tokenizer stores every 3-character substring, so a MATCH built from
the query's own trigrams (OR-joined) finds a document that shares enough
character trigrams with the query even when no whole word matches. This module
holds:

- ``TRIGRAM_FTS_SCHEMA``: the ``fts_trigram`` virtual-table DDL, appended to the
  index schema so it is created (and, via the tmp-DB build + ``os.replace``,
  swapped) atomically with the primary FTS table.
- ``build_trigram_match_expr``: turn a raw query into a safe OR-of-trigrams
  ``MATCH`` expression (each trigram is a quoted phrase so user input cannot
  inject FTS operators). Returns ``None`` when the query has no trigram (i.e. is
  shorter than 3 chars), so the caller safely no-ops the fallback.
- ``trigram_fallback_enabled`` / ``trigram_fallback_threshold``: env config,
  mirroring ``cooccurrence.py``. The fallback is OFF by default so existing
  search behaviour is unchanged; it only backfills when the PRIMARY FTS query
  returns at or below the threshold number of hits.

Ranking discipline: the trigram fallback is used ONLY as a backfill. The primary
FTS hits keep their bm25 order and top positions; trigram-only hits are appended
strictly after them. So an exact/primary match is never diluted or reordered by a
fuzzy hit. See ``Index.search`` for the wiring.
"""
from __future__ import annotations

import os

# Secondary FTS5 table tokenized with ``trigram``. Same indexed columns as the
# primary ``fts`` table (id UNINDEXED so it round-trips the doc id without being
# matched). The default ``detail=full`` is required: the trigram fallback builds
# an OR of quoted-trigram PHRASE queries, and FTS5 rejects phrase queries when
# ``detail`` is anything other than ``full``. snippet()/highlight() offsets are
# not used off this table (the primary table supplies snippets for returned
# hits), but phrase support is what we need here.
TRIGRAM_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_trigram USING fts5(
    id UNINDEXED,
    title,
    tags,
    applies_when,
    description,
    body,
    tokenize='trigram'
);
"""

# Minimum characters for a trigram. A query shorter than this yields no trigram
# and the fallback safely no-ops (an empty MATCH would otherwise error on FTS5).
_TRIGRAM_LEN = 3

# Default number of hits at or below which the primary query is considered "few"
# and the trigram fallback fires to backfill.
DEFAULT_FALLBACK_THRESHOLD = 3


def _query_trigrams(query: str) -> list[str]:
    """Return the distinct 3-char substrings of ``query`` (lower-cased).

    Whitespace runs are collapsed so trigrams do not straddle a space (matching
    how the FTS5 trigram tokenizer treats the stored text: it emits trigrams
    within token boundaries). Order-stable and de-duplicated.
    """
    grams: list[str] = []
    seen: set[str] = set()
    for word in query.lower().split():
        for i in range(len(word) - _TRIGRAM_LEN + 1):
            g = word[i : i + _TRIGRAM_LEN]
            if g not in seen:
                seen.add(g)
                grams.append(g)
    return grams


def build_trigram_match_expr(query: str) -> str | None:
    """Build an OR-of-trigrams FTS5 ``MATCH`` expression, or None if not viable.

    Each trigram is emitted as a quoted phrase (embedded quotes doubled) so the
    query cannot break out of the phrase or inject FTS5 operators. OR-joining the
    trigrams means a document matches when it shares ANY trigram with the query,
    ranked by bm25 so a document sharing MORE trigrams (a near-miss of the real
    term) ranks ahead of one sharing only a few. Returns ``None`` when the query
    is shorter than a single trigram, so the caller no-ops the fallback.
    """
    grams = _query_trigrams(query)
    if not grams:
        return None
    return " OR ".join('"' + g.replace('"', '""') + '"' for g in grams)


def trigram_fallback_enabled() -> bool:
    """Whether the trigram fuzzy fallback is active. Default OFF (safe default).

    ``KB_TRIGRAM_MODE=on`` (case-insensitive) enables it; any other value
    (including unset) leaves it off so existing search behaviour is unchanged.
    """
    return os.getenv("KB_TRIGRAM_MODE", "off").strip().lower() == "on"


def trigram_fallback_threshold() -> int:
    """Hit-count threshold below/at which the trigram fallback fires.

    From ``KB_TRIGRAM_FALLBACK_THRESHOLD``; defaults to
    ``DEFAULT_FALLBACK_THRESHOLD``. A non-negative int; a malformed or negative
    value falls back to the default rather than misconfiguring the fallback.
    """
    raw = os.getenv("KB_TRIGRAM_FALLBACK_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_FALLBACK_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_FALLBACK_THRESHOLD
    return value if value >= 0 else DEFAULT_FALLBACK_THRESHOLD
