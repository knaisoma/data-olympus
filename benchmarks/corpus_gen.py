"""Deterministic synthetic corpus generator.

Honesty: this corpus is SYNTHETIC, generated, and does not represent any real
KB. It exists to exercise scale, supersession chains, and type/status diversity
under controlled, reproducible conditions.

De-leaking (0.3.0): the earlier generator wrote the lifecycle words a query
searches for straight into the doc it was supposed to retrieve. Stale docs said
"previous", current docs said "current", and titles carried "(old)"/"(current)"
qualifiers — the exact words the ``status``/``graph`` queries used ("current
rule for X", "what replaced the previous X guidance"). A keyword method could
then win those categories by echoing a string rather than by understanding
lifecycle. The lifecycle signal now lives ONLY in the ``status`` frontmatter and
the ``supersedes``/``superseded_by`` chain (which is where a real KB carries
it); the body prose is lifecycle-neutral and shared across the old/new pair,
plus every doc mixes in a pool of shared "distractor" vocabulary so a query term
is not a near-unique fingerprint of its gold doc. Remaining known leak: the
``exact`` query still echoes the topic word, which also appears in the title and
body. That is intentional and documented in ``benchmarks/README.md`` — ``exact``
is meant to be the literal-term category; it is not claimed to measure anything
harder than keyword lookup.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

from benchmarks.corpus_model import Concept, CorpusManifest, TopicRecord

if TYPE_CHECKING:
    from pathlib import Path

TOPICS = [
    "caching", "retries", "pagination", "rate-limiting", "idempotency",
    "logging", "tracing", "secrets-handling", "input-validation", "auth-tokens",
    "database-migrations", "connection-pooling", "feature-flags", "circuit-breakers",
    "message-ordering", "schema-evolution", "backpressure", "graceful-shutdown",
    "health-checks", "config-reloading", "error-budgets", "canary-rollouts",
    "blue-green-deploys", "dead-letter-queues", "saga-orchestration",
]

# Shared "distractor" vocabulary sprinkled into every body. These are common
# engineering words that appear across many docs, so a body is not a near-unique
# bag of its own topic terms; a keyword method must actually rank on the topic
# signal rather than trivially isolating one document. Deterministically sampled
# per doc from the seeded RNG.
_SHARED_VOCAB = [
    "reliability", "latency", "throughput", "consistency", "observability",
    "resilience", "scalability", "maintainability", "operability", "durability",
    "rollback", "deployment", "configuration", "monitoring", "alerting",
    "dependency", "contract", "boundary", "invariant", "guardrail",
    "review", "rationale", "tradeoff", "constraint", "convention",
]

# (tier, type) assignments cycle so the corpus spans all tiers and types.
_TIERS = ["T1", "T2", "T3", "T4", "meta"]
_TYPES = ["standard", "decision", "workflow", "project", "reference", "memory"]
_DIR_FOR_TIER = {
    "T1": "universal/foundation",
    "T2": "tech-stacks/backend-nestjs",
    "T3": "projects/example-project",
    "T4": "projects/example-project/components/api",
    "meta": "decisions",
}

_SUPERSEDE_FRACTION = 0.15


def _body(topic: str, distractors: list[str]) -> str:
    """Lifecycle-neutral body for a topic.

    Contains the topic word (so the ``exact`` category still works) plus shared
    distractor vocabulary, but deliberately NO lifecycle words ("previous",
    "current", "replaced", "old", "new"). The old-vs-new distinction is carried
    entirely by ``status`` frontmatter and the supersedes chain, so lifecycle
    queries cannot be answered by string-echo of a qualifier written into the
    body. The prose is identical for the old and new doc of a supersession pair.
    """
    d = ", ".join(distractors)
    return (
        f"# {topic}\n\n"
        f"This concept defines the governance for {topic}. "
        f"When working with {topic}, follow the rules below. "
        f"The {topic} approach affects {d}.\n\n"
        f"- Prefer the documented {topic} pattern.\n"
        f"- Record exceptions to the {topic} rule.\n"
        f"- Weigh the {distractors[0]} and {distractors[-1]} implications.\n"
    )


def _frontmatter(c: Concept, extra_fields: list[str] | None = None) -> str:
    """Build frontmatter block for a concept.

    Builds field lines as a list, then joins them — no string-splice fragility.
    extra_fields contains pre-formatted 'key: value' strings to insert before
    the closing fence.
    """
    lines = [
        "---",
        f"id: {c.id}",
        f"type: {c.type}",
        f"status: {c.status}",
        f"tier: {c.tier}",
        f"title: {c.title}",
    ]
    if extra_fields:
        lines.extend(extra_fields)
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def generate_corpus(dest: Path, *, n: int = 250, seed: int = 0) -> CorpusManifest:
    rng = random.Random(seed)
    concepts: list[Concept] = []
    topics: list[TopicRecord] = []

    count = 0
    topic_idx = 0
    while count < n:
        topic = TOPICS[topic_idx % len(TOPICS)]
        # Disambiguate repeated topics across cycles.
        suffix = topic_idx // len(TOPICS)
        topic_key = topic if suffix == 0 else f"{topic}-{suffix}"
        tier = _TIERS[topic_idx % len(_TIERS)]
        ctype = _TYPES[topic_idx % len(_TYPES)]
        directory = _DIR_FOR_TIER[tier]
        make_pair = rng.random() < _SUPERSEDE_FRACTION and count + 1 < n

        # Deterministically pick shared distractor vocab for this topic. The old
        # and new doc of a pair share the SAME body (including distractors), so
        # the only differences between them are id, status, title suffix, and the
        # supersedes/superseded_by chain — never the searchable prose.
        distractors = rng.sample(_SHARED_VOCAB, k=4)
        body = _body(topic_key, distractors)

        if make_pair:
            old_id = f"BENCH-OLD-{topic_key}".upper()
            new_id = f"BENCH-NEW-{topic_key}".upper()
            old = Concept(
                id=old_id, path=f"{directory}/{old_id}.md", tier=tier, type=ctype,
                status="superseded", title=f"{topic_key}", topic=topic_key,
                body=body,
            )
            new = Concept(
                id=new_id, path=f"{directory}/{new_id}.md", tier=tier, type=ctype,
                status="active", title=f"{topic_key}", topic=topic_key,
                body=body,
            )
            concepts.extend([old, new])
            topics.append(TopicRecord(topic_key, current_id=new_id, current_type=ctype,
                                      stale_id=old_id))
            count += 2
        else:
            cid = f"BENCH-{topic_key}".upper()
            status = "active" if ctype != "decision" else "accepted"
            concepts.append(Concept(
                id=cid, path=f"{directory}/{cid}.md", tier=tier, type=ctype,
                status=status, title=f"{topic_key}", topic=topic_key,
                body=body,
            ))
            topics.append(TopicRecord(topic_key, current_id=cid, current_type=ctype,
                                      stale_id=None))
            count += 1
        topic_idx += 1

    _write(dest, concepts)
    return CorpusManifest(concepts=concepts, topics=topics)


def _write(dest: Path, concepts: list[Concept]) -> None:
    for c in concepts:
        p = dest / c.path
        p.parent.mkdir(parents=True, exist_ok=True)
        # Build supersession fields as a list to avoid frontmatter splice bugs.
        extra: list[str] = []
        if c.id.startswith("BENCH-OLD-"):
            new_id = c.id.replace("BENCH-OLD-", "BENCH-NEW-")
            extra.append(f"superseded_by: {new_id}")
        elif c.id.startswith("BENCH-NEW-"):
            old_id = c.id.replace("BENCH-NEW-", "BENCH-OLD-")
            extra.append(f"supersedes: {old_id}")
        fm = _frontmatter(c, extra_fields=extra if extra else None)
        p.write_text(fm + c.body, encoding="utf-8")
