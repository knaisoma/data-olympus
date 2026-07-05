"""Stratified governance scenario query generator.

Strata:
- trigger_covered: query uses a trigger term from applies_when; gold = current_id.
- paraphrase_uncovered: query uses ONLY held-out intent phrasings (no trigger
  term); gold = current_id. data-olympus is expected to lose here.
- supersession: "what is the current rule for <topic>"; gold = {current_id, stale_id}.
- negative: scenario about a distractor topic with NO governing doc; gold = [].

The paraphrase_uncovered stratum is where the benchmark is honest: if curated
applies_when metadata does not help, the numbers will show that plainly.

yaml round-trip: write_governance_queries / load_governance_queries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from benchmarks.governance_corpus import GovCorpusManifest


@dataclass(frozen=True)
class GovQuery:
    """A single governance scenario query with ground-truth metadata."""

    text: str
    stratum: str          # trigger_covered | paraphrase_uncovered | supersession | negative
    gold_ids: list[str]   # empty list for negative queries
    current_id: str = ""  # the canonical current governing doc (empty for negatives)
    stale_id: str = ""    # the superseded doc id (empty unless supersession stratum)


# ---------------------------------------------------------------------------
# Paraphrase template expansions
# ---------------------------------------------------------------------------

def _trigger_query(covered_terms: list[str]) -> str:
    """Build a trigger-covered query from trigger terms ONLY (no topic name).

    The gold doc's body and title do not contain the trigger terms, so the doc
    is reachable only via its applies_when metadata. This isolates the marginal
    value of indexing applies_when over body-only FTS.
    """
    terms = covered_terms[:2]
    return "I am using " + " and ".join(terms)


# Pre-built, intent-only sentences per intent phrase.
# These must not contain any trigger term word when split on whitespace.
_UNCOVERED_TEMPLATES: list[str] = [
    "How should I handle {}?",
    "What is the approved approach for {}?",
    "Looking for guidance on {}.",
    "Best practice question about {}.",
    "Our team needs to {}.",
]


def _uncovered_query(intent_phrase: str, idx: int) -> str:
    """Build an uncovered paraphrase query using only held-out intent phrasings."""
    template = _UNCOVERED_TEMPLATES[idx % len(_UNCOVERED_TEMPLATES)]
    return template.format(intent_phrase)


def _supersession_query(topic: str) -> str:
    """Build a supersession query."""
    return f"What is the current governing rule for {topic}?"


def _negative_query(distractor: str) -> str:
    """Build a negative query (no governing doc exists)."""
    return f"What is the team standard for {distractor.replace('-', ' ')}?"


def _fts_tokens(text: str) -> set[str]:
    """Individual lowercase tokens as FTS would see them (split on non-alnum).

    Trigger leakage must be checked at THIS granularity, not against whole
    trigger strings: a multi-word trigger like "cursor pagination" contributes
    the tokens "cursor" and "pagination" to the FTS index, so an uncovered query
    containing the single word "pagination" would still match the gold doc's
    applies_when column. Comparing only whole trigger strings misses that.
    """
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _trigger_tokens(covered_terms: list[str]) -> set[str]:
    """All FTS tokens contributed by a topic's trigger terms."""
    toks: set[str] = set()
    for term in covered_terms:
        toks |= _fts_tokens(term)
    return toks


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_governance_queries(manifest: GovCorpusManifest) -> list[GovQuery]:
    """Build all four query strata from a GovCorpusManifest.

    Integrity check (token-level): a ``paraphrase_uncovered`` query must share NO
    FTS token with its own gold doc's trigger terms. This is stricter than the
    earlier whole-string check, which let a multi-word trigger ("cursor
    pagination") leak a single token ("pagination") into an "uncovered" query and
    inflate the held-out recall. Queries that cannot be made token-disjoint are
    skipped, so "held-out paraphrase" genuinely means the applies_when metadata
    cannot help. The test asserts the same invariant.
    """
    # Trigger tokens per topic (own-doc), for the strict disjointness check.
    trig_tokens_by_id: dict[str, set[str]] = {
        topic.current_id: _trigger_tokens(topic.covered_terms)
        for topic in manifest.topics
    }

    queries: list[GovQuery] = []

    for _i, topic in enumerate(manifest.topics):
        own_trigger_tokens = trig_tokens_by_id[topic.current_id]

        # --- trigger_covered ---
        if topic.covered_terms:
            tq = _trigger_query(topic.covered_terms)
            queries.append(GovQuery(
                text=tq,
                stratum="trigger_covered",
                gold_ids=[topic.current_id],
                current_id=topic.current_id,
            ))

        # --- paraphrase_uncovered ---
        for j, intent_phrase in enumerate(topic.uncovered_terms):
            uq = _uncovered_query(intent_phrase, j)
            # Integrity: the query must share NO FTS token with its own gold
            # doc's triggers. If the intent phrase itself carries a trigger
            # token, no template can fix it, so skip the phrase entirely.
            if _fts_tokens(uq) & own_trigger_tokens:
                continue
            queries.append(GovQuery(
                text=uq,
                stratum="paraphrase_uncovered",
                gold_ids=[topic.current_id],
                current_id=topic.current_id,
            ))

        # --- supersession ---
        if topic.stale_id is not None:
            sq = _supersession_query(topic.topic)
            queries.append(GovQuery(
                text=sq,
                stratum="supersession",
                gold_ids=[topic.current_id, topic.stale_id],
                current_id=topic.current_id,
                stale_id=topic.stale_id,
            ))

    # --- negative ---
    for distractor in manifest.distractor_topics:
        nq = _negative_query(distractor)
        queries.append(GovQuery(
            text=nq,
            stratum="negative",
            gold_ids=[],
        ))

    return queries


# ---------------------------------------------------------------------------
# yaml round-trip
# ---------------------------------------------------------------------------

def write_governance_queries(queries: list[GovQuery], dest: Path) -> None:
    """Write queries to a yaml file."""
    import yaml  # type: ignore[import-untyped]

    records = [
        {
            "text": q.text,
            "stratum": q.stratum,
            "gold_ids": q.gold_ids,
            "current_id": q.current_id,
            "stale_id": q.stale_id,
        }
        for q in queries
    ]
    dest.write_text(yaml.safe_dump(records, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def load_governance_queries(src: Path) -> list[GovQuery]:
    """Load queries from a yaml file written by write_governance_queries."""
    import yaml  # type: ignore[import-untyped]

    records = yaml.safe_load(src.read_text(encoding="utf-8")) or []
    return [
        GovQuery(
            text=r["text"],
            stratum=r["stratum"],
            gold_ids=r["gold_ids"],
            current_id=r.get("current_id", ""),
            stale_id=r.get("stale_id", ""),
        )
        for r in records
    ]

