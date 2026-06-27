"""Policy core for enforcement: the heuristic intent classifier and the
in-memory consultation ledger. Pure and dependency-free so it is unit-testable
without a FastMCP server."""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
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

# Command fragments (matched against action_diff) that indicate a governed
# dependency/install action, so Bash/shell tool actions can be classified.
GOVERNED_COMMAND_PATTERNS: tuple[str, ...] = (
    "pip install", "pip3 install", "uv add", "uv pip install", "poetry add",
    "npm install", "npm i ", "yarn add", "pnpm add",
    "apt install", "apt-get install", "brew install",
    "go get", "cargo add", "gem install", "bundle add",
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
        command_patterns: tuple[str, ...] = GOVERNED_COMMAND_PATTERNS,
    ) -> None:
        self._keywords = tuple(k.lower() for k in keywords)
        self._path_globs = tuple(path_globs)
        self._command_patterns = tuple(p.lower() for p in command_patterns)
        # Pre-compile a word-boundary regex per keyword so "authored" does not
        # match "auth". \b around each keyword; keywords with spaces still work.
        self._keyword_res = tuple(
            (kw, re.compile(rf"\b{re.escape(kw)}\b")) for kw in self._keywords
        )

    def classify(
        self,
        *,
        intent: str = "",
        action_path: str | None = None,
        action_diff: str = "",
    ) -> ClassifyResult:
        signals: list[str] = []
        text = f"{intent} {action_diff}".lower()
        for kw, rx in self._keyword_res:
            if rx.search(text):
                signals.append(f"keyword:{kw}")
        diff_lower = action_diff.lower()
        for pat in self._command_patterns:
            if pat in diff_lower:
                signals.append(f"command:{pat.strip()}")
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


log = logging.getLogger("data_olympus")


class ConsultationLedger:
    """Records which (session, workspace) pairs consulted and when.

    With no ``path`` it is purely in-memory (the slice-1 behavior). With a
    ``path`` it loads an existing JSON file on construction and rewrites it on
    every ``record`` so consultations survive a server restart. A corrupt or
    unreadable file degrades to empty with a logged warning and never crashes."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._entries: dict[tuple[str, str], LedgerEntry] = {}
        if path:
            self._load()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                rows = json.load(f)
            for row in rows:
                key = (row["session_id"], row["workspace"])
                self._entries[key] = LedgerEntry(
                    consulted_at=float(row["consulted_at"]),
                    rule_ids=list(row.get("rule_ids", [])),
                )
        except Exception as exc:  # noqa: BLE001 - corrupt file -> empty, never crash
            log.warning("consultation ledger at %s unreadable, starting empty: %s",
                        self._path, exc)
            self._entries = {}

    def _save(self) -> None:
        if not self._path:
            return
        rows = [
            {"session_id": s, "workspace": w,
             "consulted_at": e.consulted_at, "rule_ids": e.rule_ids}
            for (s, w), e in self._entries.items()
        ]
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(rows, f)

    def record(
        self, *, session_id: str, workspace: str, rule_ids: list[str], now: float
    ) -> None:
        self._entries[(session_id, workspace)] = LedgerEntry(
            consulted_at=now, rule_ids=list(rule_ids)
        )
        self._save()

    def is_fresh(
        self, *, session_id: str, workspace: str, now: float, ttl_sec: float
    ) -> bool:
        entry = self._entries.get((session_id, workspace))
        if entry is None:
            return False
        return (now - entry.consulted_at) <= ttl_sec

    def get(self, *, session_id: str, workspace: str) -> LedgerEntry | None:
        return self._entries.get((session_id, workspace))
