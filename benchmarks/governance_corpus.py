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
    "rate-limiting": (
        ["token bucket", "leaky bucket", "429 response", "rate limit header"],
        ["throttle callers", "cap requests per second", "protect the api"],
    ),
    "connection-pooling": (
        ["pgbouncer", "pool size", "max connections", "connection pool"],
        ["reuse database connections", "too many db connections", "pool exhaustion"],
    ),
    "feature-flags": (
        ["launchdarkly", "flag toggle", "kill switch", "gradual rollout flag"],
        ["turn a feature on and off", "dark launch", "runtime toggle"],
    ),
    "pagination": (
        ["cursor pagination", "offset limit", "page token", "keyset pagination"],
        ["return results in pages", "paginate a large list", "next page link"],
    ),
    "caching-strategy": (
        ["cache-aside", "write-through", "ttl eviction", "cache invalidation"],
        ["store computed results", "avoid recomputation", "speed up reads"],
    ),
    "retry-policy": (
        ["exponential backoff", "jitter", "max attempts", "retry budget"],
        ["reattempt failed calls", "handle transient errors", "flaky dependency"],
    ),
    "schema-evolution": (
        ["avro schema", "backward compatible", "schema registry", "field deprecation"],
        ["change a data contract", "add a field safely", "evolve the payload"],
    ),
    "dead-letter-queues": (
        ["dlq", "poison message", "redrive policy", "max receive count"],
        ["capture unprocessable messages", "handle failed events", "quarantine bad data"],
    ),
    "tracing": (
        ["opentelemetry", "trace context", "span propagation", "w3c traceparent"],
        ["follow a request across services", "distributed trace", "correlation id"],
    ),
    "config-management": (
        ["configmap", "env overlay", "hot reload", "config precedence"],
        ["change settings without redeploy", "environment specific config", "runtime config"],
    ),
    "backpressure": (
        ["bounded queue", "reactive streams", "flow control", "buffer limit"],
        ["slow down producers", "overwhelmed consumer", "prevent memory blowup"],
    ),
    "blue-green-deploys": (
        ["blue green", "traffic cutover", "environment swap", "instant rollback"],
        ["zero downtime release", "switch between environments", "safe deploy"],
    ),
    "saga-orchestration": (
        ["saga pattern", "compensating transaction", "orchestrator", "choreography"],
        ["distributed transaction", "roll back across services", "multi step workflow"],
    ),
    "api-versioning": (
        ["url versioning", "accept header version", "sunset header", "deprecation policy"],
        ["evolve a public api", "breaking change to endpoint", "support old clients"],
    ),
    "audit-logging": (
        ["audit trail", "tamper evident log", "actor attribution", "immutable log"],
        ["record sensitive actions", "compliance logging", "track access"],
    ),
}

# "Distractor" topic names — no governing doc exists (used for negative queries).
# A governance tool should ABSTAIN on these; the negative stratum measures the
# false-positive rate. Grown to >= 30 so the FP-rate estimate has a usable
# denominator and a bootstrap CI that is not degenerate.
_DISTRACTOR_TOPICS: list[str] = [
    "microservice-naming",
    "test-naming-conventions",
    "ui-color-palette",
    "sprint-ceremony-schedule",
    "team-offsite-planning",
    "desk-seating-plan",
    "coffee-machine-rota",
    "slack-emoji-etiquette",
    "meeting-room-booking",
    "parking-allocation",
    "onboarding-buddy-pairing",
    "lunch-vendor-rotation",
    "conference-travel-budget",
    "swag-order-process",
    "birthday-celebration-policy",
    "standup-time-preference",
    "keyboard-brand-preference",
    "monitor-arm-selection",
    "office-plant-care",
    "whiteboard-marker-restocking",
    "friday-demo-signup",
    "book-club-selection",
    "hackathon-team-formation",
    "mentorship-matching",
    "internal-newsletter-cadence",
    "wiki-gardening-schedule",
    "photo-wall-curation",
    "team-mascot-naming",
    "retro-format-choice",
    "pto-calendar-color",
    "chair-ergonomics-survey",
]

# Fraction of governance topics that get a superseded predecessor. Realised as
# ``(topic_idx + seed) % 3 == 0`` below, which yields >= 10 supersession pairs at
# 30 topics (grown from the earlier %6 that produced only ~3-5). This keeps the
# supersession stratum large enough for a non-degenerate CI.
_SUPERSEDE_FRACTION = 0.33
_SUPERSEDE_MODULUS = 3

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

def _body(topic: str, qualifier: str) -> str:
    """Build a doc body that describes the governance rule in prose WITHOUT
    repeating the applies_when trigger terms.

    The triggers live only in the applies_when frontmatter, so the benchmark
    fairly measures whether indexing applies_when (vs body-only FTS) improves
    retrieval. This mirrors reality: a standard's prose describes the recommended
    approach, while the curated triggers list the intents/tools that should
    activate the rule (often the to-avoid options, which the prose need not name).
    """
    return (
        f"# {topic} ({qualifier})\n\n"
        f"This document records the {qualifier} governance decision for the "
        f"{topic} area. It states the recommended approach, the rationale behind "
        f"it, and how to request an exception.\n\n"
        f"## Decision\n\n"
        f"- Adopt the documented pattern for this area.\n"
        f"- Prefer the recommended approach over ad-hoc alternatives.\n"
        f"- Record any deviation in the project decision record.\n"
        f"- Consult this page before related work begins.\n"
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

    topic_keys = list(_GOV_TOPICS.keys())

    concepts: list[Concept] = []
    topic_records: list[GovTopicRecord] = []

    # Unique topics only (no suffix repeats): each governing doc covers a
    # distinct topic with a distinct trigger vocabulary, so a trigger-only query
    # identifies exactly one gold doc. n caps the number of topics used.
    n_topics = min(n, len(topic_keys))
    for topic_idx, key in enumerate(topic_keys[:n_topics]):
        topic_key = key

        trigger_vocab, intent_vocab = _GOV_TOPICS[key]
        # Guarantee disjointness: the two lists come from the fixed table which
        # was designed with disjoint terms. We make a defensive copy.
        covered = list(trigger_vocab)
        uncovered = list(intent_vocab)

        tier = _TIERS[topic_idx % len(_TIERS)]
        doc_type = _TYPES[topic_idx % len(_TYPES)]
        status = _STATUSES[doc_type]

        # Deterministic supersession pairs by position (seed shifts the phase),
        # so a fixed fraction of topics always have a superseded predecessor.
        make_pair = ((topic_idx + seed) % _SUPERSEDE_MODULUS == 0)

        if make_pair:
            old_id = f"GOV-OLD-{topic_key}".upper().replace("-", "_")
            new_id = f"GOV-NEW-{topic_key}".upper().replace("-", "_")
            directory = _DIR_FOR_TIER[tier]

            old_body = _body(topic_key, "previous")
            new_body = _body(topic_key, "current")

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
        else:
            doc_id = f"GOV-{topic_key}".upper().replace("-", "_")
            directory = _DIR_FOR_TIER[tier]
            body = _body(topic_key, "current")

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

    return GovCorpusManifest(
        concepts=concepts,
        topics=topic_records,
        distractor_topics=list(_DISTRACTOR_TOPICS),
    )
