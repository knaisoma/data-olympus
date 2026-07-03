"""Deterministic query + gold-label generator.

Emits four query categories from a CorpusManifest:
- exact: literal topic term (e.g. "caching")
- semantic: a paraphrase with low literal overlap (synonym map)
- status: "current rule for <topic>" for supersession topics
- graph: "what replaced the previous <topic> guidance" for supersession topics

Each BenchQuery records text, category, gold_ids (the correct concept ids),
current_id, and stale_id (None when no supersession).

De-leaking note (0.3.0): the corpus bodies no longer contain the lifecycle words
these templates use ("current", "previous", "replaced"). Those words are now
pure natural-language framing that matches NOTHING in the corpus, so a keyword
method cannot answer a lifecycle query by echoing a qualifier written into the
gold doc. What actually resolves a ``status``/``graph`` query is the topic term
(shared by the old and new doc of the pair) plus the retriever's ability to keep
the superseded doc out of the answer — which is exactly what the staleness
metric measures. The old and new doc are now string-identical except for
``status`` and the supersedes chain, so a status-blind ranker has NO lexical way
to prefer the current doc; only a status-aware retriever can.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from benchmarks.corpus_model import CorpusManifest

# Fixed synonym map for base topics. Used by the semantic category to produce
# paraphrases with minimal literal overlap with the concept body.
_SEMANTIC: dict[str, str] = {
    "caching": "storing computed results to avoid recomputation",
    "retries": "automatically reattempting failed requests",
    "pagination": "splitting large result sets across multiple responses",
    "rate-limiting": "throttling requests to protect downstream capacity",
    "idempotency": "ensuring repeated operations produce the same result",
    "logging": "structured recording of runtime events for observability",
    "tracing": "propagating correlation context across service boundaries",
    "secrets-handling": "secure management of credentials and sensitive keys",
    "input-validation": "verifying user-supplied data before processing",
    "auth-tokens": "issuing and verifying bearer credentials for identity",
    "database-migrations": "versioned schema changes applied incrementally",
    "connection-pooling": "reusing persistent connections across requests",
    "feature-flags": "toggling behavior at runtime without deployment",
    "circuit-breakers": "halting calls to failing dependencies automatically",
    "message-ordering": "preserving event sequence in async pipelines",
    "schema-evolution": "backward-compatible changes to data contracts",
    "backpressure": "signaling producers to slow when consumers are overwhelmed",
    "graceful-shutdown": "draining in-flight work before process termination",
    "health-checks": "exposing readiness and liveness probes for orchestrators",
    "config-reloading": "applying configuration updates without restart",
    "error-budgets": "quantified tolerance for service unreliability",
    "canary-rollouts": "incrementally shifting traffic to a new version",
    "blue-green-deploys": "switching live traffic between two identical environments",
    "dead-letter-queues": "capturing unprocessable messages for later inspection",
    "saga-orchestration": "coordinating distributed transactions via compensating steps",
}


def _semantic_phrase(base_topic: str) -> str:
    """Return a low-overlap paraphrase for the base topic."""
    return _SEMANTIC.get(base_topic, f"approach for {base_topic}")


@dataclass(frozen=True)
class BenchQuery:
    text: str
    category: str           # "exact" | "semantic" | "status" | "graph"
    gold_ids: list[str]     # correct concept ids (usually [current_id])
    current_id: str
    stale_id: str | None


def build_queries(manifest: CorpusManifest) -> list[BenchQuery]:
    """Build the full query set from a CorpusManifest.

    Each topic in the manifest contributes at least two queries (exact,
    semantic). Topics with a supersession pair also contribute status and graph
    queries.
    """
    queries: list[BenchQuery] = []
    for t in manifest.topics:
        # Derive the base topic for synonym lookup: strip trailing numeric suffix.
        # e.g. "caching-1" -> base "caching"; "caching" -> "caching"
        base = t.topic.rsplit("-", 1)[0] if t.topic[-1].isdigit() else t.topic

        # exact: literal topic words
        queries.append(BenchQuery(
            text=t.topic.replace("-", " "),
            category="exact",
            gold_ids=[t.current_id],
            current_id=t.current_id,
            stale_id=t.stale_id,
        ))
        # semantic: synonym paraphrase
        queries.append(BenchQuery(
            text=_semantic_phrase(base),
            category="semantic",
            gold_ids=[t.current_id],
            current_id=t.current_id,
            stale_id=t.stale_id,
        ))
        if t.stale_id is not None:
            # status: gold = current; stale must be ignored by good methods
            queries.append(BenchQuery(
                text=f"current rule for {t.topic.replace('-', ' ')}",
                category="status",
                gold_ids=[t.current_id],
                current_id=t.current_id,
                stale_id=t.stale_id,
            ))
            # graph: "what replaced" the old guidance
            queries.append(BenchQuery(
                text=f"what replaced the previous {t.topic.replace('-', ' ')} guidance",
                category="graph",
                gold_ids=[t.current_id],
                current_id=t.current_id,
                stale_id=t.stale_id,
            ))
    return queries


def write_queries(queries: list[BenchQuery], path: Path) -> None:
    """Serialize queries to YAML. Each query is a mapping with all fields."""
    records = [
        {
            "text": q.text,
            "category": q.category,
            "gold_ids": q.gold_ids,
            "current_id": q.current_id,
            "stale_id": q.stale_id,
        }
        for q in queries
    ]
    path.write_text(yaml.safe_dump(records, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def load_queries(path: Path) -> list[BenchQuery]:
    """Deserialize queries from a YAML file written by write_queries."""
    records = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [
        BenchQuery(
            text=r["text"],
            category=r["category"],
            gold_ids=r["gold_ids"],
            current_id=r["current_id"],
            stale_id=r.get("stale_id"),
        )
        for r in records
    ]
