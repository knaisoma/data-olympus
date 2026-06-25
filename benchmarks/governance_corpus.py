"""Deterministic governance corpus generator.

Each document governs one TOPIC drawn from a fixed table. The table defines:
- trigger_vocab: terms authored onto the doc's `applies_when` (covered).
- intent_vocab: real-world phrasings held out (uncovered) — never written to
  the doc body, so the paraphrase_uncovered query stratum genuinely tests
  whether the retrieval system bridges the lexical gap.

Honesty guardrail: covered_terms and uncovered_terms are DISJOINT by
construction (drawn from fully separate lists per topic). Tests assert this.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from benchmarks.corpus_model import Concept

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Governance topic table
# Each entry: topic_key -> (trigger_vocab, intent_vocab)
# trigger_vocab  = terms authored onto applies_when (covered_terms)
# intent_vocab   = held-out real-world phrasings (uncovered_terms)
# The two sets are DISJOINT — verified by construction and by test.
# ---------------------------------------------------------------------------
_GOV_TOPICS: dict[str, tuple[list[str], list[str]]] = {
    "excel-export": (
        ["openpyxl", "xlsxwriter", "insert_cols", "insert_rows", "xlsx"],
        ["spreadsheet workbook", "cell formulas", "write to excel file"],
    ),
    "force-push": (
        ["force-push", "--force", "push -f", "force_with_lease"],
        ["overwrite remote history", "rewrite the branch", "undo a published commit"],
    ),
    "module-structure": (
        ["nestjs module", "feature module", "providers array", "imports array"],
        ["organize the backend", "where to put services", "project layout"],
    ),
    "secrets-handling": (
        ["secrets manager", "vault", "dotenv", "env var", "credentials file"],
        ["store passwords", "keep api keys safe", "protect sensitive config"],
    ),
    "database-migrations": (
        ["flyway", "alembic", "migration script", "schema version"],
        ["change the database schema", "add a column", "rename a table"],
    ),
    "input-validation": (
        ["pydantic", "zod", "class-validator", "dto validation"],
        ["check user input", "sanitize request data", "validate form fields"],
    ),
    "auth-tokens": (
        ["jwt", "access token", "refresh token", "bearer token"],
        ["user login session", "keep a user logged in", "authenticate api calls"],
    ),
    "logging": (
        ["structlog", "pino", "log level", "structured logging"],
        ["record events", "write application logs", "audit trail"],
    ),
    "error-budgets": (
        ["slo", "error budget", "burn rate", "sli metric"],
        ["how much downtime is acceptable", "reliability target", "uptime goal"],
    ),
    "canary-rollouts": (
        ["canary deploy", "traffic split", "progressive delivery", "rollout percentage"],
        ["release to a small group first", "gradual rollout", "test in production"],
    ),
    "circuit-breakers": (
        ["circuit breaker", "half-open state", "failure threshold", "fallback"],
        ["stop calling a failing service", "handle downstream outages"],
    ),
    "message-ordering": (
        ["kafka partition", "message ordering", "consumer group", "at-least-once"],
        ["ensure messages arrive in order", "process events sequentially"],
    ),
    "graceful-shutdown": (
        ["sigterm", "drain connections", "shutdown hook", "preStop"],
        ["stop the app cleanly", "finish in-flight requests before exiting"],
    ),
    "health-checks": (
        ["liveness probe", "readiness probe", "healthz endpoint", "k8s probe"],
        ["is the service up", "kubernetes pod restart", "load balancer check"],
    ),
    "idempotency": (
        ["idempotency key", "deduplication id", "at-most-once", "retry safe"],
        ["duplicate request", "safe to retry", "send twice"],
    ),
}

# "Distractor" topic names — no governing doc exists (used for negative queries).
_DISTRACTOR_TOPICS: list[str] = [
    "microservice-naming",
    "test-naming-conventions",
    "ui-color-palette",
    "sprint-ceremony-schedule",
    "team-offsite-planning",
]

_SUPERSEDE_FRACTION = 0.15

_TIERS = ["T1", "T2", "T3", "T4"]
_TYPES = ["standard", "decision", "workflow", "reference"]
_STATUSES = {
    "standard": "active",
    "decision": "accepted",
    "workflow": "active",
    "reference": "active",
}
_DIR_FOR_TIER = {
    "T1": "universal/foundation",
    "T2": "tech-stacks/backend",
    "T3": "projects/example-project",
    "T4": "projects/example-project/components/api",
}


# ---------------------------------------------------------------------------
# Extended TopicRecord for governance (adds covered_terms / uncovered_terms)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GovTopicRecord:
    """Ground truth for one governance topic.

    covered_terms  - trigger terms authored onto the doc's applies_when.
    uncovered_terms - held-out intent phrasings NOT authored anywhere in the
                      doc body (guaranteed disjoint from covered_terms).
    """

    topic: str
    current_id: str
    covered_terms: list[str]
    uncovered_terms: list[str]
    stale_id: str | None = None


@dataclass(frozen=True)
class GovCorpusManifest:
    """Manifest returned by generate_governance_corpus."""

    concepts: list[Concept] = field(default_factory=list)
    topics: list[GovTopicRecord] = field(default_factory=list)
    distractor_topics: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Document body helpers
# ---------------------------------------------------------------------------

def _body(topic: str, covered_terms: list[str], qualifier: str) -> str:
    """Build a doc body that contains the covered trigger terms but NOT the
    uncovered intent phrasings (which are held out for the paraphrase stratum).
    """
    triggers_inline = ", ".join(covered_terms[:3])
    return (
        f"# {topic} ({qualifier})\n\n"
        f"This document governs the {qualifier} rules for {topic}. "
        f"Applies when working with: {triggers_inline}.\n\n"
        f"## Guidance\n\n"
        f"- Follow the documented {topic} pattern.\n"
        f"- Use the approved tools ({triggers_inline}) as described.\n"
        f"- Record exceptions to the {topic} rule in the project decision log.\n"
        f"- Review this document before starting a {topic} implementation.\n"
    )


def _frontmatter(
    doc_id: str,
    doc_type: str,
    status: str,
    tier: str,
    title: str,
    description: str,
    applies_when: list[str],
    extra_fields: list[str] | None = None,
) -> str:
    """Build YAML frontmatter block. applies_when is emitted as a YAML list."""
    lines = [
        "---",
        f"id: {doc_id}",
        f"type: {doc_type}",
        f"status: {status}",
        f"tier: {tier}",
        f"title: {title}",
        f"description: {description}",
    ]
    if applies_when:
        lines.append("applies_when:")
        for term in applies_when:
            # Wrap in quotes to handle hyphens/spaces safely in YAML.
            lines.append(f'  - "{term}"')
    if extra_fields:
        lines.extend(extra_fields)
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _write_doc(
    dest: Path,
    doc_id: str,
    doc_type: str,
    status: str,
    tier: str,
    title: str,
    description: str,
    applies_when: list[str],
    body: str,
    extra_fields: list[str] | None = None,
) -> None:
    directory = _DIR_FOR_TIER[tier]
    p = dest / directory / f"{doc_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = _frontmatter(doc_id, doc_type, status, tier, title, description,
                      applies_when, extra_fields)
    p.write_text(fm + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Public generator
# ---------------------------------------------------------------------------

def generate_governance_corpus(
    dest: Path, *, n: int = 120, seed: int = 0
) -> GovCorpusManifest:
    """Generate a deterministic governance corpus with applies_when triggers.

    Returns a GovCorpusManifest. The manifest's ``topics`` list contains
    GovTopicRecord objects, each with ``covered_terms`` (authored onto
    applies_when) and ``uncovered_terms`` (held out, not in the doc body).

    Integrity guardrail: covered_terms and uncovered_terms are disjoint by
    construction (from separate GOV_TOPICS columns). This is asserted by test.
    """
    from pathlib import Path as _Path
    dest = _Path(dest)

    rng = random.Random(seed)
    topic_keys = list(_GOV_TOPICS.keys())

    concepts: list[Concept] = []
    topic_records: list[GovTopicRecord] = []

    count = 0
    topic_idx = 0
    while count < n:
        key = topic_keys[topic_idx % len(topic_keys)]
        suffix = topic_idx // len(topic_keys)
        topic_key = key if suffix == 0 else f"{key}-{suffix}"

        trigger_vocab, intent_vocab = _GOV_TOPICS[key]
        # Guarantee disjointness: the two lists come from the fixed table which
        # was designed with disjoint terms. We make a defensive copy.
        covered = list(trigger_vocab)
        uncovered = list(intent_vocab)

        tier = _TIERS[topic_idx % len(_TIERS)]
        doc_type = _TYPES[topic_idx % len(_TYPES)]
        status = _STATUSES[doc_type]

        make_pair = rng.random() < _SUPERSEDE_FRACTION and count + 1 < n

        if make_pair:
            old_id = f"GOV-OLD-{topic_key}".upper().replace("-", "_")
            new_id = f"GOV-NEW-{topic_key}".upper().replace("-", "_")
            directory = _DIR_FOR_TIER[tier]

            old_body = _body(topic_key, covered, "previous")
            new_body = _body(topic_key, covered, "current")

            old_concept = Concept(
                id=old_id,
                path=f"{directory}/{old_id}.md",
                tier=tier,
                type=doc_type,
                status="superseded",
                title=f"{topic_key} (old)",
                topic=topic_key,
                body=old_body,
            )
            new_concept = Concept(
                id=new_id,
                path=f"{directory}/{new_id}.md",
                tier=tier,
                type=doc_type,
                status=status,
                title=f"{topic_key} (current)",
                topic=topic_key,
                body=new_body,
            )
            concepts.extend([old_concept, new_concept])
            topic_records.append(GovTopicRecord(
                topic=topic_key,
                current_id=new_id,
                covered_terms=covered,
                uncovered_terms=uncovered,
                stale_id=old_id,
            ))

            _write_doc(
                dest, old_id, doc_type, "superseded", tier,
                f"{topic_key} (old)",
                f"Previous {topic_key} governance (superseded).",
                covered,
                old_body,
                extra_fields=[f"superseded_by: {new_id}"],
            )
            _write_doc(
                dest, new_id, doc_type, status, tier,
                f"{topic_key} (current)",
                f"Current {topic_key} governance.",
                covered,
                new_body,
                extra_fields=[f"supersedes: {old_id}"],
            )
            count += 2
        else:
            doc_id = f"GOV-{topic_key}".upper().replace("-", "_")
            directory = _DIR_FOR_TIER[tier]
            body = _body(topic_key, covered, "current")

            concepts.append(Concept(
                id=doc_id,
                path=f"{directory}/{doc_id}.md",
                tier=tier,
                type=doc_type,
                status=status,
                title=topic_key,
                topic=topic_key,
                body=body,
            ))
            topic_records.append(GovTopicRecord(
                topic=topic_key,
                current_id=doc_id,
                covered_terms=covered,
                uncovered_terms=uncovered,
                stale_id=None,
            ))

            _write_doc(
                dest, doc_id, doc_type, status, tier,
                topic_key,
                f"Governance rules for {topic_key}.",
                covered,
                body,
            )
            count += 1

        topic_idx += 1

    return GovCorpusManifest(
        concepts=concepts,
        topics=topic_records,
        distractor_topics=list(_DISTRACTOR_TOPICS),
    )
