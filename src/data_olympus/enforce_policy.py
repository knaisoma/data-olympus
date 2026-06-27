"""Policy core for enforcement: the heuristic intent classifier and the
in-memory consultation ledger. Pure and dependency-free so it is unit-testable
without a FastMCP server."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

# Keyword signals that a user prompt is a governed code/architectural decision.
GOVERNED_KEYWORDS: tuple[str, ...] = (
    "library", "dependency", "dependencies", "framework", "package",
    "pattern", "migration", "migrate", "refactor", "architecture",
    "api design", "endpoint", "schema", "auth", "authorization",
    "authentication", "rls", "secret", "convention", "standard",
)

# Path globs that mark a pending file action as a governed decision. Deliberately
# narrow (dependency manifests, migrations, schema, container/build config) so
# that ordinary source edits flow without a consult; the prompt-level classifier
# carries the broad net.
GOVERNED_PATH_GLOBS: tuple[str, ...] = (
    "pyproject.toml", "*/pyproject.toml",
    "package.json", "*/package.json",
    "*/requirements*.txt", "requirements*.txt",
    "*/go.mod", "go.mod", "*/Cargo.toml", "Cargo.toml", "*/pom.xml",
    "*/migrations/*", "*/migration/*",
    "*/schema/*", "*/schema.sql", "*.sql",
    "Dockerfile", "*/Dockerfile", "*/docker-compose*.yml", "docker-compose*.yml",
)


@dataclass(frozen=True)
class ClassifyResult:
    """Outcome of a classification: governed or not, plus the matched signals."""

    is_governed_decision: bool
    signals: list[str] = field(default_factory=list)


class IntentClassifier:
    """Heuristic classifier. Pluggable: a future LLM-backed classifier can
    implement the same ``classify`` signature without touching callers."""

    def __init__(
        self,
        *,
        keywords: tuple[str, ...] = GOVERNED_KEYWORDS,
        path_globs: tuple[str, ...] = GOVERNED_PATH_GLOBS,
    ) -> None:
        self._keywords = tuple(k.lower() for k in keywords)
        self._path_globs = tuple(path_globs)

    def classify(
        self,
        *,
        intent: str = "",
        action_path: str | None = None,
        action_diff: str = "",
    ) -> ClassifyResult:
        signals: list[str] = []
        text = f"{intent} {action_diff}".lower()
        for kw in self._keywords:
            if kw in text:
                signals.append(f"keyword:{kw}")
        if action_path:
            p = action_path.replace("\\", "/")
            base = p.rsplit("/", 1)[-1]
            for glob in self._path_globs:
                if fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(base, glob):
                    signals.append(f"path:{glob}")
                    break
        return ClassifyResult(is_governed_decision=bool(signals), signals=signals)


@dataclass
class LedgerEntry:
    """A recorded consultation for a (session, workspace) pair."""

    consulted_at: float
    rule_ids: list[str]


class ConsultationLedger:
    """In-memory record of which (session, workspace) pairs have consulted and
    when. Single-replica server, so a process-local dict is sufficient. Not
    persisted across restarts (a restart simply forces the next governed edit to
    re-consult)."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], LedgerEntry] = {}

    def record(
        self, *, session_id: str, workspace: str, rule_ids: list[str], now: float
    ) -> None:
        self._entries[(session_id, workspace)] = LedgerEntry(
            consulted_at=now, rule_ids=list(rule_ids)
        )

    def is_fresh(
        self, *, session_id: str, workspace: str, now: float, ttl_sec: float
    ) -> bool:
        entry = self._entries.get((session_id, workspace))
        if entry is None:
            return False
        return (now - entry.consulted_at) <= ttl_sec

    def get(self, *, session_id: str, workspace: str) -> LedgerEntry | None:
        return self._entries.get((session_id, workspace))
