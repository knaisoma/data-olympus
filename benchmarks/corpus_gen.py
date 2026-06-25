"""Deterministic synthetic corpus generator.

Honesty: this corpus is SYNTHETIC, generated, and does not represent any real
KB. It exists to exercise scale, supersession chains, and type/status diversity
under controlled, reproducible conditions.
"""
from __future__ import annotations

import random
from pathlib import Path

from benchmarks.corpus_model import Concept, CorpusManifest, TopicRecord

TOPICS = [
    "caching", "retries", "pagination", "rate-limiting", "idempotency",
    "logging", "tracing", "secrets-handling", "input-validation", "auth-tokens",
    "database-migrations", "connection-pooling", "feature-flags", "circuit-breakers",
    "message-ordering", "schema-evolution", "backpressure", "graceful-shutdown",
    "health-checks", "config-reloading", "error-budgets", "canary-rollouts",
    "blue-green-deploys", "dead-letter-queues", "saga-orchestration",
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


def _body(topic: str, qualifier: str) -> str:
    return (
        f"# {topic} ({qualifier})\n\n"
        f"This concept defines the {qualifier} guidance for {topic}. "
        f"When working with {topic}, follow the {qualifier} rules below. "
        f"The {topic} approach affects reliability and developer ergonomics.\n\n"
        f"- Prefer the documented {topic} pattern.\n"
        f"- Record exceptions to the {topic} rule.\n"
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

        if make_pair:
            old_id = f"BENCH-OLD-{topic_key}".upper()
            new_id = f"BENCH-NEW-{topic_key}".upper()
            old = Concept(
                id=old_id, path=f"{directory}/{old_id}.md", tier=tier, type=ctype,
                status="superseded", title=f"{topic_key} (old)", topic=topic_key,
                body=_body(topic_key, "previous"),
            )
            new = Concept(
                id=new_id, path=f"{directory}/{new_id}.md", tier=tier, type=ctype,
                status="active", title=f"{topic_key} (current)", topic=topic_key,
                body=_body(topic_key, "current"),
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
                body=_body(topic_key, "current"),
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
