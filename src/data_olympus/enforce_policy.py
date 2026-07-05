"""Policy core for enforcement: the heuristic intent classifier and the
in-memory consultation ledger. Pure and dependency-free so it is unit-testable
without a FastMCP server."""
from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import os
import re
import tempfile
import threading
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
        # Free-text keyword signals always scan `intent` (a short, deliberate
        # statement of what the agent is about to do - kb_consult's argument -
        # so a keyword there is a real signal). They scan `action_diff` too,
        # but ONLY when there is no `action_path`: `action_diff` is arbitrary
        # write/command CONTENT up to 4000 chars (Write.content, Edit.new_string,
        # Bash.command, OpenCode patch text), and when a concrete action_path is
        # already given, path-glob matching below is the intended signal for
        # that action - layering the free-text net on top blocked benign writes
        # to ungoverned files that merely discussed a keyword as a topic (a
        # scratch file quoting "RLS"/"auth" from a security standard). Bash
        # commands and OpenCode's `patch` tool carry no action_path at all
        # (their action_diff IS the only content available), so the free-text
        # net still applies there - dropping it entirely would silently lose
        # the only governed-decision signal for those two tool shapes.
        text = intent.lower() if action_path else f"{intent} {action_diff}".lower()
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


# Consultation trigger provenance. "explicit" is a deliberate agent call to
# kb_consult (the MCP tool default, and any old client that sends no trigger).
# "prompt_hook" is an installer-driven UserPromptSubmit/BeforeAgent auto-consult:
# recorded for audit/compliance but NOT sufficient to clear the gate, because it
# fires on every user turn and would otherwise keep the ledger perpetually fresh.
EXPLICIT_TRIGGER = "explicit"
PROMPT_HOOK_TRIGGER = "prompt_hook"


@dataclass
class LedgerEntry:
    """A recorded consultation for a (session, workspace) pair.

    ``consulted_at`` is the last touch of ANY trigger (used for retention/TTL of
    the row itself and audit). ``explicit_at`` is the last EXPLICIT consult, or
    None if only prompt-hook auto-consults have been recorded. The gate clears on
    ``explicit_at`` freshness so a prompt-hook auto-consult (which fires every
    turn) can never satisfy the gate, while an explicit consult is not silently
    downgraded by a later prompt-hook consult on the same (session, workspace)."""

    consulted_at: float
    rule_ids: list[str]
    explicit_at: float | None = None


log = logging.getLogger("data_olympus")


class ConsultationLedger:
    """Records which (session, workspace) pairs consulted and when.

    With no ``path`` it is purely in-memory (the slice-1 behavior). With a
    ``path`` it loads an existing JSON file on construction and rewrites it on
    every ``record`` so consultations survive a server restart. A corrupt or
    unreadable file degrades to empty with a logged warning and never crashes."""

    def __init__(
        self,
        path: str | None = None,
        *,
        retention_sec: float = 3600.0,
        max_entries: int = 50_000,
    ) -> None:
        self._path = path
        self._entries: dict[tuple[str, str], LedgerEntry] = {}
        # An entry is a TTL freshness cache: once it is older than the consult
        # TTL it can never be fresh again, so we evict it on the next record().
        # ``retention_sec`` should be >= the ttl_sec passed to is_fresh (the
        # server threads config.consult_ttl_sec in) so a still-fresh entry is
        # never dropped. ``max_entries`` is a hard belt-and-suspenders cap that
        # bounds memory/disk even under a flood of unique sessions inside one
        # retention window. Together they stop _entries growing without bound and
        # keep the per-record file rewrite O(active window) instead of O(all
        # sessions ever seen).
        self._retention_sec = retention_sec
        self._max_entries = max_entries
        # record() mutates _entries and rewrites the whole file; once consult
        # handlers are offloaded to the anyio threadpool these can run
        # concurrently. Without this lock, _save()'s iteration over _entries can
        # race a concurrent insert (RuntimeError / dropped entries). Public
        # methods take the lock; _save()/_evict() do not (called only while held).
        self._lock = threading.Lock()
        if path:
            self._load()
            # A ledger persisted by the previous unbounded implementation can be
            # arbitrarily large; cap it on load so an oversized /state/ledger.json
            # is not held in memory until the first record() prunes it. TTL
            # eviction still runs on the first record() (it needs a caller "now").
            self._enforce_cap()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                rows = json.load(f)
            for row in rows:
                key = (row["session_id"], row["workspace"])
                # Back-compat: a ledger persisted before the trigger split has no
                # explicit_at. Treat those legacy rows as explicit (they were all
                # gate-clearing under the old policy), so a server upgrade does not
                # spuriously re-block a session that already consulted.
                consulted_at = float(row["consulted_at"])
                explicit_at = row.get("explicit_at", consulted_at)
                self._entries[key] = LedgerEntry(
                    consulted_at=consulted_at,
                    rule_ids=list(row.get("rule_ids", [])),
                    explicit_at=None if explicit_at is None else float(explicit_at),
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
             "consulted_at": e.consulted_at, "rule_ids": e.rule_ids,
             "explicit_at": e.explicit_at}
            for (s, w), e in self._entries.items()
        ]
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        # Atomic write: serialize to a temp file in the same directory, then
        # os.replace() over the target so a crash/full-disk mid-write cannot
        # truncate or corrupt the existing ledger.
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rows, f)
            os.replace(tmp, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _evict(self, now: float) -> None:
        """Drop entries that can no longer be fresh, then enforce the hard cap.

        Caller must hold ``self._lock``. Reassigns ``self._entries`` to a pruned
        dict; O(n) but n is exactly what this bounds.
        """
        cutoff = now - self._retention_sec
        self._entries = {
            key: e for key, e in self._entries.items() if e.consulted_at >= cutoff
        }
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        """Bound _entries to the newest ``max_entries`` by consulted_at. Clock-free
        so it can run on load (before any caller "now" is available). Caller holds
        the lock, except the single-threaded construction-time call."""
        if len(self._entries) > self._max_entries:
            newest = sorted(
                self._entries.items(), key=lambda kv: kv[1].consulted_at, reverse=True
            )[: self._max_entries]
            self._entries = dict(newest)

    def record(
        self,
        *,
        session_id: str,
        workspace: str,
        rule_ids: list[str],
        now: float,
        trigger: str = EXPLICIT_TRIGGER,
    ) -> None:
        """Record a consultation. ``trigger`` is EXPLICIT_TRIGGER (a deliberate
        agent consult that clears the gate) or PROMPT_HOOK_TRIGGER (an installer
        auto-consult that is audited but never clears the gate).

        A prompt-hook record refreshes ``consulted_at`` (row liveness/audit) but
        carries forward any existing ``explicit_at`` so it cannot downgrade a
        still-fresh explicit consult into a non-clearing one."""
        with self._lock:
            prior = self._entries.get((session_id, workspace))
            if trigger == EXPLICIT_TRIGGER:
                explicit_at: float | None = now
            else:
                explicit_at = prior.explicit_at if prior is not None else None
            self._entries[(session_id, workspace)] = LedgerEntry(
                consulted_at=now, rule_ids=list(rule_ids), explicit_at=explicit_at
            )
            self._evict(now)
            self._save()

    def is_fresh(
        self,
        *,
        session_id: str,
        workspace: str,
        now: float,
        ttl_sec: float,
        require_explicit: bool = True,
    ) -> bool:
        """True when a fresh consult is on record for (session, workspace).

        With ``require_explicit`` (the gate's default) only an explicit consult
        within ``ttl_sec`` counts: a prompt-hook auto-consult never clears the
        gate. With ``require_explicit=False`` any consult (explicit or prompt
        hook) within the TTL counts (used where the mere fact of a recent consult,
        not its provenance, matters)."""
        with self._lock:
            entry = self._entries.get((session_id, workspace))
        if entry is None:
            return False
        ts = entry.explicit_at if require_explicit else entry.consulted_at
        if ts is None:
            return False
        return (now - ts) <= ttl_sec

    def get(self, *, session_id: str, workspace: str) -> LedgerEntry | None:
        with self._lock:
            return self._entries.get((session_id, workspace))
