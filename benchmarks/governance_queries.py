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


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_governance_queries(manifest: GovCorpusManifest) -> list[GovQuery]:
    """Build all four query strata from a GovCorpusManifest.

    Integrity check: asserts that no paraphrase_uncovered query contains any
    word that is itself a complete trigger string (the test also asserts this).
    """
    all_trigger_strings: set[str] = {
        t.lower() for topic in manifest.topics for t in topic.covered_terms
    }

    queries: list[GovQuery] = []

    for _i, topic in enumerate(manifest.topics):
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
            # Integrity: verify no word in the query is a complete trigger string.
            query_words = set(uq.lower().split())
            overlap = query_words & all_trigger_strings
            if overlap:
                # Fall back to a safer template that avoids the collision.
                uq = f"Guidance needed for {intent_phrase} in our project."
                query_words = set(uq.lower().split())
                overlap = query_words & all_trigger_strings
                if overlap:
                    # Skip this particular phrase (can't build a clean query).
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

