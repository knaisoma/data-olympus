# Enforcement Core (slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn data-olympus from advisory into an enforced gate by adding a consultation ledger, a shared intent classifier, a gate-check verdict, compliance audit, a Claude Code hook dispatcher, and a `kb enforce` install/uninstall/status/doctor CLI.

**Architecture:** A server-side policy core (`enforce_policy.py` for the classifier + in-memory consultation ledger; `tools_enforce.py` for the `kb_consult` / `kb_gate_check` / `kb_compliance` functions) reuses the existing index and audit log. The functions are registered as MCP tools in `server.py` and mirrored as REST routes in `rest_api.py`, following the exact pattern of the existing read/write tools. A bash hook dispatcher (`bin/kb-enforce-hook`) drives the loop from Claude Code's SessionStart / UserPromptSubmit / PreToolUse / Stop hooks, and a Python helper (`bin/_kb_enforce.py`) installs/uninstalls that wiring idempotently into `~/.claude/settings.json` behind a managed block, fronted by a new `kb enforce` subcommand.

**Tech Stack:** Python 3.13, pydantic v2, FastMCP, Starlette routes, pytest + pytest-asyncio + httpx, bash + bats.

**Spec:** `docs/superpowers/specs/2026-06-27-mandatory-consultation-enforcement-design.md`

**Conventions to follow (verified against the codebase):**
- Tool functions live in `tools_*.py`, take all deps as keyword-only args (`*,`), and return a pydantic model from `models.py`. The server `@app.tool()` wrapper calls the `_fn` and returns `resp.model_dump()`.
- REST handlers are added inside `register_routes()` in `rest_api.py` via `@app.custom_route(...)`.
- `from __future__ import annotations` at the top of every module.
- Run tests with `uv run pytest`; lint with `uv run ruff check .`; bats lives under `tests/`.
- **Every task that changes behaviour updates `CHANGELOG.md` `[Unreleased]` — this is mandatory (`.rules/changelog-per-release.md`).** Task 13 is the single consolidated changelog + docs entry; do not skip it.

---

### Task 1: Response models for consult / gate / compliance

**Files:**
- Modify: `src/data_olympus/models.py` (append new models after `AuditResponse`, before `RenameCandidateModel`)
- Test: `tests/test_enforce_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enforce_models.py
"""Tests for enforcement response models."""
from __future__ import annotations

from data_olympus.models import (
    ComplianceResponse,
    ConsultResponse,
    GateCheckResponse,
    SearchHitModel,
)


def test_consult_response_round_trips() -> None:
    hit = SearchHitModel(id="STD-U-002", path="universal/foundation/STD-U-002.md",
                         title="Writing style", snippet="...", score=1.0)
    resp = ConsultResponse(is_governed_decision=True, rules=[hit],
                           consulted_at=100.0, ttl_seconds=300)
    dumped = resp.model_dump()
    assert dumped["is_governed_decision"] is True
    assert dumped["rules"][0]["id"] == "STD-U-002"
    assert dumped["ttl_seconds"] == 300


def test_gate_check_response_defaults() -> None:
    resp = GateCheckResponse(verdict="allow")
    assert resp.verdict == "allow"
    assert resp.reason == ""
    assert resp.rules == []


def test_compliance_response_defaults() -> None:
    resp = ComplianceResponse()
    assert resp.counts == {}
    assert resp.by_agent == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enforce_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'ConsultResponse'`

- [ ] **Step 3: Add the models**

Append to `src/data_olympus/models.py` immediately after the `AuditResponse` class:

```python
class ConsultResponse(BaseModel):
    """kb_consult response: governing rules plus a recorded consultation."""

    is_governed_decision: bool
    rules: list[SearchHitModel] = []
    consulted_at: float
    ttl_seconds: int


class GateCheckResponse(BaseModel):
    """kb_gate_check verdict for a pending code action."""

    verdict: str  # 'allow' | 'consult_required'
    reason: str = ""
    rules: list[SearchHitModel] = []


class ComplianceResponse(BaseModel):
    """Aggregated enforcement-event counts overall and per agent."""

    counts: dict[str, int] = {}
    by_agent: dict[str, dict[str, int]] = {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enforce_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/models.py tests/test_enforce_models.py
git commit -m "feat(enforce): add consult/gate/compliance response models"
```

---

### Task 2: Intent classifier

**Files:**
- Create: `src/data_olympus/enforce_policy.py`
- Test: `tests/test_enforce_classifier.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enforce_classifier.py
"""Tests for the heuristic intent classifier."""
from __future__ import annotations

from data_olympus.enforce_policy import IntentClassifier


def test_keyword_in_intent_is_governed() -> None:
    c = IntentClassifier()
    r = c.classify(intent="should we add a new logging library here?")
    assert r.is_governed_decision is True
    assert any(s.startswith("keyword:") for s in r.signals)


def test_plain_chat_is_not_governed() -> None:
    c = IntentClassifier()
    r = c.classify(intent="what time is it?")
    assert r.is_governed_decision is False
    assert r.signals == []


def test_dependency_manifest_path_is_governed() -> None:
    c = IntentClassifier()
    r = c.classify(action_path="/home/me/proj/pyproject.toml")
    assert r.is_governed_decision is True
    assert any(s.startswith("path:") for s in r.signals)


def test_ordinary_source_edit_is_not_governed_by_path_alone() -> None:
    c = IntentClassifier()
    r = c.classify(action_path="/home/me/proj/src/util/strings.py")
    assert r.is_governed_decision is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enforce_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'data_olympus.enforce_policy'`

- [ ] **Step 3: Create the classifier**

```python
# src/data_olympus/enforce_policy.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enforce_classifier.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/enforce_policy.py tests/test_enforce_classifier.py
git commit -m "feat(enforce): add heuristic intent classifier"
```

---

### Task 3: Consultation ledger

**Files:**
- Modify: `src/data_olympus/enforce_policy.py` (append the ledger)
- Test: `tests/test_enforce_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enforce_ledger.py
"""Tests for the in-memory consultation ledger."""
from __future__ import annotations

from data_olympus.enforce_policy import ConsultationLedger


def test_record_then_fresh_within_ttl() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="proj", rule_ids=["STD-U-002"], now=1000.0)
    assert led.is_fresh(session_id="s1", workspace="proj", now=1100.0, ttl_sec=300.0)


def test_stale_after_ttl() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="proj", rule_ids=[], now=1000.0)
    assert not led.is_fresh(session_id="s1", workspace="proj", now=1400.0, ttl_sec=300.0)


def test_unknown_key_is_not_fresh() -> None:
    led = ConsultationLedger()
    assert not led.is_fresh(session_id="nope", workspace="proj", now=1.0, ttl_sec=300.0)


def test_keys_are_isolated_per_session_and_workspace() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="projA", rule_ids=[], now=1000.0)
    assert not led.is_fresh(session_id="s1", workspace="projB", now=1000.0, ttl_sec=300.0)
    assert not led.is_fresh(session_id="s2", workspace="projA", now=1000.0, ttl_sec=300.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enforce_ledger.py -v`
Expected: FAIL with `ImportError: cannot import name 'ConsultationLedger'`

- [ ] **Step 3: Append the ledger to `enforce_policy.py`**

```python
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
```

Add `from dataclasses import dataclass, field` already exists at the top; no import change needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enforce_ledger.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/enforce_policy.py tests/test_enforce_ledger.py
git commit -m "feat(enforce): add in-memory consultation ledger"
```

---

### Task 4: `kb_consult_fn`

**Files:**
- Create: `src/data_olympus/tools_enforce.py`
- Test: `tests/test_tools_enforce_consult.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools_enforce_consult.py
"""Tests for kb_consult_fn."""
from __future__ import annotations

from data_olympus.audit_log import AuditLog
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.tools_enforce import kb_consult_fn


class _FakeIndex:
    """Minimal stand-in exposing the .search and .health surface kb_search_fn uses."""

    def search(self, query, limit=20, tier=None, category=None, status=None, doc_type=None):
        return []

    def health(self):
        return {"source_commit": "deadbeef"}


def test_consult_records_and_flags_governed(tmp_path) -> None:
    led = ConsultationLedger()
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    resp = kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="add a new caching library",
        source_session="s1", agent_identity="claude",
        ttl_sec=300.0, now=1000.0, audit_log=al,
    )
    assert resp.is_governed_decision is True
    assert resp.ttl_seconds == 300
    assert led.is_fresh(session_id="s1", workspace="proj", now=1000.0, ttl_sec=300.0)


def test_consult_records_even_when_not_governed(tmp_path) -> None:
    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="say hello",
        source_session="s1", agent_identity="claude",
        ttl_sec=300.0, now=1000.0, audit_log=None,
    )
    assert resp.is_governed_decision is False
    assert led.is_fresh(session_id="s1", workspace="proj", now=1000.0, ttl_sec=300.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_enforce_consult.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'data_olympus.tools_enforce'`

- [ ] **Step 3: Create `tools_enforce.py` with `kb_consult_fn`**

```python
# src/data_olympus/tools_enforce.py
"""Enforcement tool function implementations: consult, gate-check, compliance.

Decoupled from FastMCP registration, deps passed as kwargs, return pydantic
models — mirrors tools_read.py / tools_write.py."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.models import (
    ComplianceResponse,
    ConsultResponse,
    GateCheckResponse,
)
from data_olympus.tools_read import kb_search_fn

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog
    from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
    from data_olympus.index import Index

ENFORCE_EVENT_TYPES = (
    "consult", "gate_allow", "gate_block", "gate_bypass", "gate_degraded",
)


def kb_consult_fn(
    *,
    idx: Index,
    classifier: IntentClassifier,
    ledger: ConsultationLedger,
    workspace: str,
    intent: str,
    source_session: str,
    agent_identity: str,
    ttl_sec: float,
    now: float,
    audit_log: AuditLog | None = None,
    limit: int = 5,
) -> ConsultResponse:
    """Classify the intent, retrieve governing rules when governed, and record a
    consultation in the ledger keyed by (source_session, workspace)."""
    result = classifier.classify(intent=intent)
    rules = []
    rule_ids: list[str] = []
    if result.is_governed_decision:
        search = kb_search_fn(idx=idx, query=intent, limit=limit)
        rules = list(search.hits)
        rule_ids = [h.id for h in search.hits]
    ledger.record(
        session_id=source_session, workspace=workspace, rule_ids=rule_ids, now=now
    )
    if audit_log is not None:
        audit_log.append({
            "ts": now, "event_type": "consult", "status": "recorded",
            "agent_identity": agent_identity, "source_session": source_session,
            "target_path": workspace,
            "reason": ",".join(result.signals) if result.signals else "",
        })
    return ConsultResponse(
        is_governed_decision=result.is_governed_decision,
        rules=rules, consulted_at=now, ttl_seconds=int(ttl_sec),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_enforce_consult.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/tools_enforce.py tests/test_tools_enforce_consult.py
git commit -m "feat(enforce): add kb_consult_fn (classify, retrieve, record)"
```

---

### Task 5: `kb_gate_check_fn`

**Files:**
- Modify: `src/data_olympus/tools_enforce.py` (append)
- Test: `tests/test_tools_enforce_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools_enforce_gate.py
"""Tests for kb_gate_check_fn."""
from __future__ import annotations

from data_olympus.audit_log import AuditLog
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.tools_enforce import kb_gate_check_fn


def test_non_governed_action_allows_without_consult() -> None:
    resp = kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=ConsultationLedger(),
        workspace="proj", session_id="s1", tool_name="Edit",
        action_path="/p/src/util/strings.py", action_diff="",
        now=1000.0, ttl_sec=300.0,
    )
    assert resp.verdict == "allow"


def test_governed_action_without_consult_requires_consult(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    resp = kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=ConsultationLedger(),
        workspace="proj", session_id="s1", tool_name="Edit",
        action_path="/p/pyproject.toml", action_diff="",
        now=1000.0, ttl_sec=300.0, audit_log=al,
    )
    assert resp.verdict == "consult_required"
    events = list(al.iter_filtered())
    assert any(e["event_type"] == "gate_block" for e in events)


def test_governed_action_with_fresh_consult_allows(tmp_path) -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="proj", rule_ids=[], now=1000.0)
    resp = kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=led,
        workspace="proj", session_id="s1", tool_name="Edit",
        action_path="/p/pyproject.toml", action_diff="",
        now=1100.0, ttl_sec=300.0,
    )
    assert resp.verdict == "allow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_enforce_gate.py -v`
Expected: FAIL with `ImportError: cannot import name 'kb_gate_check_fn'`

- [ ] **Step 3: Append `kb_gate_check_fn` to `tools_enforce.py`**

```python
def kb_gate_check_fn(
    *,
    classifier: IntentClassifier,
    ledger: ConsultationLedger,
    workspace: str,
    session_id: str,
    tool_name: str,
    action_path: str | None,
    action_diff: str,
    now: float,
    ttl_sec: float,
    audit_log: AuditLog | None = None,
) -> GateCheckResponse:
    """Decide whether a pending code action may proceed. Governed actions require
    a fresh consultation on record for (session_id, workspace)."""
    result = classifier.classify(action_path=action_path, action_diff=action_diff)
    if not result.is_governed_decision:
        return GateCheckResponse(verdict="allow", reason="action not governed")
    fresh = ledger.is_fresh(
        session_id=session_id, workspace=workspace, now=now, ttl_sec=ttl_sec
    )
    if fresh:
        if audit_log is not None:
            audit_log.append({
                "ts": now, "event_type": "gate_allow", "status": "allow",
                "source_session": session_id, "target_path": action_path or workspace,
                "reason": ",".join(result.signals),
            })
        return GateCheckResponse(
            verdict="allow", reason="fresh consultation on record"
        )
    if audit_log is not None:
        audit_log.append({
            "ts": now, "event_type": "gate_block", "status": "consult_required",
            "source_session": session_id, "target_path": action_path or workspace,
            "reason": ",".join(result.signals),
        })
    return GateCheckResponse(
        verdict="consult_required",
        reason="governed action without a fresh consultation; call kb_consult first",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_enforce_gate.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/tools_enforce.py tests/test_tools_enforce_gate.py
git commit -m "feat(enforce): add kb_gate_check_fn verdict logic"
```

---

### Task 6: `kb_compliance_fn`

**Files:**
- Modify: `src/data_olympus/tools_enforce.py` (append)
- Test: `tests/test_tools_enforce_compliance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools_enforce_compliance.py
"""Tests for kb_compliance_fn."""
from __future__ import annotations

from data_olympus.audit_log import AuditLog
from data_olympus.tools_enforce import kb_compliance_fn


def test_compliance_counts_enforce_events_only(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({"ts": 1.0, "event_type": "consult", "status": "recorded",
               "agent_identity": "claude"})
    al.append({"ts": 2.0, "event_type": "gate_block", "status": "consult_required",
               "agent_identity": "claude"})
    al.append({"ts": 3.0, "event_type": "propose_memory", "status": "committed",
               "agent_identity": "claude"})  # not an enforce event
    resp = kb_compliance_fn(audit_log=al)
    assert resp.counts.get("consult") == 1
    assert resp.counts.get("gate_block") == 1
    assert "propose_memory" not in resp.counts
    assert resp.by_agent["claude"]["gate_block"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_enforce_compliance.py -v`
Expected: FAIL with `ImportError: cannot import name 'kb_compliance_fn'`

- [ ] **Step 3: Append `kb_compliance_fn` to `tools_enforce.py`**

```python
def kb_compliance_fn(
    *,
    audit_log: AuditLog,
    since: float | None = None,
    agent: str | None = None,
) -> ComplianceResponse:
    """Aggregate enforcement events (consult / gate_*) into overall and per-agent
    counts. Ignores non-enforcement audit events."""
    counts: dict[str, int] = {}
    by_agent: dict[str, dict[str, int]] = {}
    for ev in audit_log.iter_filtered(since=since, agent=agent):
        et = ev.get("event_type", "")
        if et not in ENFORCE_EVENT_TYPES:
            continue
        counts[et] = counts.get(et, 0) + 1
        who = ev.get("agent_identity") or "unknown"
        bucket = by_agent.setdefault(who, {})
        bucket[et] = bucket.get(et, 0) + 1
    return ComplianceResponse(counts=counts, by_agent=by_agent)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_enforce_compliance.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/tools_enforce.py tests/test_tools_enforce_compliance.py
git commit -m "feat(enforce): add kb_compliance_fn aggregation"
```

---

### Task 7: Config field for consult TTL

**Files:**
- Modify: `src/data_olympus/config.py:Config` (add field) and `load_config()` (read env)
- Test: `tests/test_config_enforce.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_enforce.py
"""Config tests for enforcement settings."""
from __future__ import annotations

from data_olympus.config import load_config


def test_consult_ttl_default(monkeypatch) -> None:
    monkeypatch.delenv("KB_CONSULT_TTL_SEC", raising=False)
    cfg = load_config()
    assert cfg.consult_ttl_sec == 300


def test_consult_ttl_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KB_CONSULT_TTL_SEC", "120")
    cfg = load_config()
    assert cfg.consult_ttl_sec == 120
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_enforce.py -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'consult_ttl_sec'`

- [ ] **Step 3: Add the field and env read**

In `src/data_olympus/config.py`, add to the `Config` dataclass after `auth_token`:

```python
    consult_ttl_sec: int = 300
```

In `load_config()`, add before the `return Config(`:

```python
    consult_ttl_sec = int(os.getenv("KB_CONSULT_TTL_SEC", "300"))
```

And add to the `Config(...)` constructor call:

```python
        consult_ttl_sec=consult_ttl_sec,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_enforce.py tests/test_config.py -v`
Expected: PASS (existing config tests still pass)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/config.py tests/test_config_enforce.py
git commit -m "feat(enforce): add KB_CONSULT_TTL_SEC config"
```

---

### Task 8: Wire policy core into the server + register MCP tools

**Files:**
- Modify: `src/data_olympus/server.py` (`ServerState.__init__`, `build_app`, `build_app_from_config`)
- Test: `tests/test_server_enforce_tools.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_enforce_tools.py
"""The enforce tools are registered and reachable on the app."""
from __future__ import annotations

import asyncio

from data_olympus.server import build_app


def test_enforce_tools_registered(tmp_kb, tmp_index_path) -> None:
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
    )
    names = {t.name for t in asyncio.run(app.get_tools()).values()}
    assert {"kb_consult", "kb_gate_check", "kb_compliance"} <= names
```

(Note: `tmp_kb` and `tmp_index_path` fixtures already exist in `tests/conftest.py`; reuse as the other server tests do.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_enforce_tools.py -v`
Expected: FAIL (assertion: enforce tool names not in set)

- [ ] **Step 3: Wire state and register tools**

In `src/data_olympus/server.py`:

1. Add imports near the existing tool imports:

```python
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
```

2. In `ServerState.__init__`, add two parameters and assignments. Add to the signature (after `audit_log`):

```python
        classifier: IntentClassifier | None = None,
        ledger: ConsultationLedger | None = None,
```

and in the body:

```python
        self.classifier: IntentClassifier = classifier or IntentClassifier()
        self.ledger: ConsultationLedger = ledger or ConsultationLedger()
```

3. In `build_app`, immediately after `state = ServerState(idx=idx, git=git, config=config)` is created, the defaults already populate `classifier`/`ledger`, so no change there. Register the tools by adding the following inside `build_app`, just before the `from data_olympus.rest_api import register_routes` line:

```python
    @app.tool()
    def kb_consult(
        workspace: str, intent: str, source_session: str,
        agent_identity: str,
    ) -> dict[str, object]:
        """Record a consultation for (source_session, workspace) and return the
        governing rules for the intent. Call before code/architectural work."""
        import time as _time
        from data_olympus.tools_enforce import kb_consult_fn
        resp = kb_consult_fn(
            idx=state.idx, classifier=state.classifier, ledger=state.ledger,
            workspace=workspace, intent=intent, source_session=source_session,
            agent_identity=agent_identity,
            ttl_sec=state.config.consult_ttl_sec, now=_time.time(),
            audit_log=state.audit_log,
        )
        return resp.model_dump()

    @app.tool()
    def kb_gate_check(
        workspace: str, session_id: str, tool_name: str,
        action_path: str | None = None, action_diff: str = "",
    ) -> dict[str, object]:
        """Return a verdict (allow | consult_required) for a pending code action.
        Governed actions require a fresh consultation on record."""
        import time as _time
        from data_olympus.tools_enforce import kb_gate_check_fn
        resp = kb_gate_check_fn(
            classifier=state.classifier, ledger=state.ledger,
            workspace=workspace, session_id=session_id, tool_name=tool_name,
            action_path=action_path, action_diff=action_diff,
            now=_time.time(), ttl_sec=state.config.consult_ttl_sec,
            audit_log=state.audit_log,
        )
        return resp.model_dump()

    @app.tool()
    def kb_compliance(
        since: float | None = None, agent: str | None = None,
    ) -> dict[str, object]:
        """Aggregate enforcement events (consult / gate_*) overall and per agent."""
        if state.audit_log is None:
            return {"counts": {}, "by_agent": {}}
        from data_olympus.tools_enforce import kb_compliance_fn
        resp = kb_compliance_fn(audit_log=state.audit_log, since=since, agent=agent)
        return resp.model_dump()
```

(No change required in `build_app_from_config`; it calls `build_app`, which now registers the tools.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server_enforce_tools.py tests/test_server_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/server.py tests/test_server_enforce_tools.py
git commit -m "feat(enforce): register kb_consult/kb_gate_check/kb_compliance MCP tools"
```

---

### Task 9: REST endpoints for consult / gate / compliance

**Files:**
- Modify: `src/data_olympus/rest_api.py` (add routes inside `register_routes`)
- Test: `tests/test_rest_enforce.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rest_enforce.py
"""REST tests for the enforcement endpoints."""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app


@pytest.fixture
def http_app(tmp_kb, tmp_index_path, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    env = {**os.environ}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy",
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pq"),
        audit_log_path=str(tmp_path / "audit.log"),
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_consult_then_gate_allows(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        c = await client.post("/api/v1/consult", json={
            "workspace": "proj", "intent": "add a new caching library",
            "source_session": "s1", "agent_identity": "claude"})
        assert c.status_code == 200
        g = await client.post("/api/v1/gate/check", json={
            "workspace": "proj", "session_id": "s1", "tool_name": "Edit",
            "action_path": "/p/pyproject.toml"})
    assert g.json()["verdict"] == "allow"


@pytest.mark.asyncio
async def test_gate_without_consult_requires_consult(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        g = await client.post("/api/v1/gate/check", json={
            "workspace": "proj", "session_id": "fresh", "tool_name": "Edit",
            "action_path": "/p/pyproject.toml"})
    assert g.json()["verdict"] == "consult_required"


@pytest.mark.asyncio
async def test_compliance_reports_events(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v1/consult", json={
            "workspace": "proj", "intent": "migration plan",
            "source_session": "s1", "agent_identity": "claude"})
        r = await client.get("/api/v1/compliance")
    assert r.json()["counts"].get("consult", 0) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rest_enforce.py -v`
Expected: FAIL (404 on `/api/v1/consult`)

- [ ] **Step 3: Add the routes**

In `src/data_olympus/rest_api.py`, inside `register_routes`, after the `audit` route, add:

```python
    @app.custom_route("/api/v1/consult", methods=["POST"])
    async def consult(request: Request) -> JSONResponse:
        import time as _time
        body = await request.json()
        from data_olympus.tools_enforce import kb_consult_fn
        resp = kb_consult_fn(
            idx=state.idx, classifier=state.classifier, ledger=state.ledger,
            workspace=body["workspace"], intent=body.get("intent", ""),
            source_session=body["source_session"],
            agent_identity=body.get("agent_identity", "unknown"),
            ttl_sec=state.config.consult_ttl_sec, now=_time.time(),
            audit_log=state.audit_log,
        )
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/gate/check", methods=["POST"])
    async def gate_check(request: Request) -> JSONResponse:
        import time as _time
        body = await request.json()
        from data_olympus.tools_enforce import kb_gate_check_fn
        resp = kb_gate_check_fn(
            classifier=state.classifier, ledger=state.ledger,
            workspace=body["workspace"], session_id=body["session_id"],
            tool_name=body.get("tool_name", ""),
            action_path=body.get("action_path"),
            action_diff=body.get("action_diff", ""),
            now=_time.time(), ttl_sec=state.config.consult_ttl_sec,
            audit_log=state.audit_log,
        )
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/compliance", methods=["GET"])
    async def compliance(request: Request) -> JSONResponse:
        if state.audit_log is None:
            return JSONResponse({"counts": {}, "by_agent": {}})
        from data_olympus.tools_enforce import kb_compliance_fn
        qp = request.query_params
        since = float(qp["since"]) if qp.get("since") else None
        agent = qp.get("agent")
        resp = kb_compliance_fn(audit_log=state.audit_log, since=since, agent=agent)
        return JSONResponse(resp.model_dump())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rest_enforce.py tests/test_rest_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/rest_api.py tests/test_rest_enforce.py
git commit -m "feat(enforce): add /consult, /gate/check, /compliance REST routes"
```

---

### Task 10: Claude Code hook dispatcher `bin/kb-enforce-hook`

**Files:**
- Create: `bin/kb-enforce-hook` (bash, executable)
- Create: `tests/cli-fixtures/enforce-mock-server.py` (mock REST for the hook)
- Create: `tests/test_kb_enforce_hook.bats`

The dispatcher reads the Claude Code hook payload on stdin (JSON with `session_id`, `cwd`, `prompt`, `tool_name`, `tool_input`). Contracts:
- `session-start`: print a one-line readiness note; exit 0.
- `user-prompt`: POST `/api/v1/consult`; print returned rules to stdout (added to context); exit 0. Fail-open on error.
- `pre-tool`: POST `/api/v1/gate/check`; on `consult_required` print reason to stderr and exit 2 (blocks the tool call); on `allow` exit 0. On unreachable: exit 0 with a stderr warning unless `KB_ENFORCE_FAIL_MODE=closed` (then exit 2).
- `stop`: exit 0.

- [ ] **Step 1: Write the failing test**

```bash
# tests/test_kb_enforce_hook.bats
#!/usr/bin/env bats
# bats tests for bin/kb-enforce-hook against a mock REST server.

setup_file() {
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export FIXTURE_DIR="${BATS_TEST_FILENAME%/*}/cli-fixtures"
  export HOOK="${REPO_ROOT}/bin/kb-enforce-hook"
}

setup() {
  PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  export PORT
  export KB_ENDPOINT="http://127.0.0.1:${PORT}"
  python3 "${FIXTURE_DIR}/enforce-mock-server.py" "$PORT" &
  MOCK_PID=$!
  export MOCK_PID
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${PORT}/api/v1/compliance" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done
}

teardown() {
  kill "$MOCK_PID" 2>/dev/null || true
  wait "$MOCK_PID" 2>/dev/null || true
}

@test "user-prompt mode prints injected rules and exits 0" {
  run bash -c 'echo "{\"session_id\":\"s1\",\"cwd\":\"/tmp/proj\",\"prompt\":\"add a library\"}" | "'"$HOOK"'" user-prompt'
  [ "$status" -eq 0 ]
  [[ "$output" == *"GOVERNING RULES"* ]]
}

@test "pre-tool mode blocks (exit 2) when consult_required" {
  run bash -c 'echo "{\"session_id\":\"blockme\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
  [[ "$output" == *"consult"* ]]
}

@test "pre-tool mode allows (exit 0) when verdict allow" {
  run bash -c 'echo "{\"session_id\":\"allowme\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/README.md\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 0 ]
}

@test "pre-tool fails open (exit 0) when endpoint unreachable" {
  KB_ENDPOINT="http://127.0.0.1:1" run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 0 ]
  [[ "$output" == *"warn"* ]] || [[ "$output" == *"unreachable"* ]]
}

@test "pre-tool fails closed (exit 2) when unreachable and KB_ENFORCE_FAIL_MODE=closed" {
  KB_ENDPOINT="http://127.0.0.1:1" KB_ENFORCE_FAIL_MODE=closed run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
}
```

Mock server fixture:

```python
# tests/cli-fixtures/enforce-mock-server.py
"""Minimal mock of the enforce REST endpoints for bats hook tests.

/api/v1/consult       -> {"is_governed_decision": true, "rules": [...], ...}
/api/v1/gate/check    -> verdict depends on action_path / session_id
/api/v1/compliance    -> {"counts": {}, "by_agent": {}}
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/v1/compliance"):
            self._send({"counts": {}, "by_agent": {}})
        else:
            self._send({"error": "not_found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or "{}")
        if self.path == "/api/v1/consult":
            self._send({
                "is_governed_decision": True,
                "rules": [{"id": "STD-U-002", "path": "p", "title": "Style",
                           "snippet": "...", "score": 1.0, "status": "", "type": ""}],
                "consulted_at": 1.0, "ttl_seconds": 300,
            })
        elif self.path == "/api/v1/gate/check":
            path = (body.get("action_path") or "")
            if path.endswith("pyproject.toml") and body.get("session_id") != "allowme":
                self._send({"verdict": "consult_required",
                            "reason": "governed action; call kb_consult first",
                            "rules": []})
            else:
                self._send({"verdict": "allow", "reason": "ok", "rules": []})
        else:
            self._send({"error": "not_found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bats tests/test_kb_enforce_hook.bats`
Expected: FAIL (`bin/kb-enforce-hook` does not exist)

- [ ] **Step 3: Create `bin/kb-enforce-hook`**

```bash
#!/usr/bin/env bash
# kb-enforce-hook - data-olympus enforcement hook dispatcher.
#
# Invoked by a coding agent's hooks. Reads the hook payload as JSON on stdin and
# talks to the data-olympus REST endpoints. Modes:
#   session-start  warm-up note (stdout -> context)
#   user-prompt    POST /consult, print governing rules (stdout -> context)
#   pre-tool       POST /gate/check, block (exit 2) on consult_required
#   stop           no-op
#
# Environment:
#   KB_ENDPOINT            REST endpoint (default http://localhost:8080)
#   KB_ENFORCE_FAIL_MODE   open|closed (default open) behaviour when unreachable
set -uo pipefail

KB_ENDPOINT="${KB_ENDPOINT:-http://localhost:8080}"
KB_ENFORCE_FAIL_MODE="${KB_ENFORCE_FAIL_MODE:-open}"
MODE="${1:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Read entire stdin payload.
PAYLOAD="$(cat || true)"

json_field() {
  # json_field <dotted.path> ; reads $PAYLOAD, prints value or empty.
  python3 -c 'import json,sys
p=sys.argv[1].split(".")
try:
    d=json.loads(sys.stdin.read() or "{}")
    for k in p:
        d=d.get(k, "") if isinstance(d, dict) else ""
    print(d if isinstance(d,str) else (d if d is None else json.dumps(d)))
except Exception:
    print("")' "$1" <<<"$PAYLOAD"
}

resolve_workspace() {
  # Echo a workspace label from cwd using the existing detector; fall back to cwd.
  local cwd="$1"
  if [[ -r "$HERE/_kb_detect_workspace.sh" ]]; then
    # shellcheck disable=SC1091
    . "$HERE/_kb_detect_workspace.sh"
    if detect_workspace_and_component "$cwd" 2>/dev/null; then
      echo "$WORKSPACE"; return 0
    fi
  fi
  echo "$cwd"
}

post_json() {
  # post_json <url> <json-body> ; prints body, returns curl status.
  curl --silent --max-time 5 -H "Content-Type: application/json" \
       --data "$2" -X POST "$1" 2>/dev/null
}

case "$MODE" in
  session-start)
    echo "[KB] data-olympus enforcement active (endpoint: $KB_ENDPOINT)."
    exit 0
    ;;
  user-prompt)
    SESSION="$(json_field session_id)"
    CWD="$(json_field cwd)"
    PROMPT="$(json_field prompt)"
    WS="$(resolve_workspace "$CWD")"
    BODY="$(python3 -c 'import json,sys
print(json.dumps({"workspace":sys.argv[1],"intent":sys.argv[2],
"source_session":sys.argv[3],"agent_identity":"claude-code"}))' "$WS" "$PROMPT" "$SESSION")"
    RESP="$(post_json "$KB_ENDPOINT/api/v1/consult" "$BODY")" || RESP=""
    if [[ -z "$RESP" ]]; then
      echo "[KB] warn: consult endpoint unreachable; proceeding without injected rules." >&2
      exit 0
    fi
    GOV="$(json_field_from "$RESP" is_governed_decision)"
    if [[ "$GOV" == "true" ]]; then
      echo "=== GOVERNING RULES (data-olympus) ==="
      echo "$RESP" | python3 -c 'import json,sys
d=json.load(sys.stdin)
for r in d.get("rules",[]):
    print(f"- {r.get(\"id\")}: {r.get(\"title\")}")'
      echo "=== consult these before deciding ==="
    fi
    exit 0
    ;;
  pre-tool)
    SESSION="$(json_field session_id)"
    CWD="$(json_field cwd)"
    TOOL="$(json_field tool_name)"
    FPATH="$(json_field tool_input.file_path)"
    WS="$(resolve_workspace "$CWD")"
    BODY="$(python3 -c 'import json,sys
print(json.dumps({"workspace":sys.argv[1],"session_id":sys.argv[2],
"tool_name":sys.argv[3],"action_path":sys.argv[4]}))' "$WS" "$SESSION" "$TOOL" "$FPATH")"
    RESP="$(post_json "$KB_ENDPOINT/api/v1/gate/check" "$BODY")" || RESP=""
    if [[ -z "$RESP" ]]; then
      if [[ "$KB_ENFORCE_FAIL_MODE" == "closed" ]]; then
        echo "[KB] gate unreachable and fail-mode=closed; blocking." >&2
        exit 2
      fi
      echo "[KB] warn: gate endpoint unreachable; failing open (enforcement gap)." >&2
      exit 0
    fi
    VERDICT="$(json_field_from "$RESP" verdict)"
    if [[ "$VERDICT" == "consult_required" ]]; then
      echo "[KB] BLOCKED: this is a governed change. Call kb_consult for '$WS' first, then retry." >&2
      exit 2
    fi
    exit 0
    ;;
  stop)
    exit 0
    ;;
  *)
    echo "kb-enforce-hook: unknown mode '$MODE' (want session-start|user-prompt|pre-tool|stop)" >&2
    exit 64
    ;;
esac
```

Add this helper near `json_field` (it parses a value out of an arbitrary JSON string instead of `$PAYLOAD`):

```bash
json_field_from() {
  # json_field_from <json-string> <key>
  python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1] or "{}")
    v=d.get(sys.argv[2], "")
    print(v if isinstance(v,str) else json.dumps(v))
except Exception:
    print("")' "$1" "$2"
}
```

Make it executable:

```bash
chmod +x bin/kb-enforce-hook
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bats tests/test_kb_enforce_hook.bats`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/kb-enforce-hook tests/test_kb_enforce_hook.bats tests/cli-fixtures/enforce-mock-server.py
git commit -m "feat(enforce): add kb-enforce-hook Claude Code dispatcher"
```

---

### Task 11: `kb enforce` installer `bin/_kb_enforce.py`

**Files:**
- Create: `bin/_kb_enforce.py` (Python, executable)
- Test: `tests/test_kb_enforce_install.py`

Installs/removes a managed block in a Claude Code settings JSON. Uses a marker key so re-runs are idempotent and uninstall is surgical. Backs up the file before editing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kb_enforce_install.py
"""Tests for the kb enforce installer (Claude Code provider)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str, settings: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args, "--settings", str(settings)],
        capture_output=True, text=True,
    )


def test_install_writes_managed_hooks(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    r = _run("install", "--agent", "claude-code", settings=settings)
    assert r.returncode == 0, r.stderr
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"  # operator content preserved
    assert "hooks" in data
    blob = json.dumps(data)
    assert "kb-enforce-hook" in blob
    assert "data-olympus-enforce" in blob  # managed marker
    assert (tmp_path / "settings.json.kb-bak").exists() or any(
        p.name.startswith("settings.json.") for p in tmp_path.iterdir()
    )


def test_install_is_idempotent(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", settings=settings)
    first = settings.read_text()
    _run("install", "--agent", "claude-code", settings=settings)
    second = settings.read_text()
    assert json.loads(first) == json.loads(second)


def test_uninstall_removes_only_managed_block(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    _run("install", "--agent", "claude-code", settings=settings)
    _run("uninstall", "--agent", "claude-code", settings=settings)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert "kb-enforce-hook" not in json.dumps(data)


def test_status_reports_installed(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", settings=settings)
    r = _run("status", "--agent", "claude-code", settings=settings)
    assert r.returncode == 0
    assert "installed" in r.stdout.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kb_enforce_install.py -v`
Expected: FAIL (`bin/_kb_enforce.py` does not exist)

- [ ] **Step 3: Create `bin/_kb_enforce.py`**

```python
#!/usr/bin/env python3
"""kb enforce installer (Claude Code provider).

Idempotently installs/removes data-olympus enforcement hook wiring inside a
Claude Code settings JSON, confined to a managed block tagged by MARKER so the
operator's own settings are never clobbered. Backs up before editing.

Subcommands: install | uninstall | status | doctor
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

MARKER = "data-olympus-enforce"  # tag stamped into each managed hook entry
SHIM_VERSION = "1"
HOOK_BIN = str(Path(__file__).resolve().parent / "kb-enforce-hook")

# Map: hook event name -> dispatcher mode + (optional) tool matcher.
HOOK_EVENTS = [
    ("SessionStart", "session-start", None),
    ("UserPromptSubmit", "user-prompt", None),
    ("PreToolUse", "pre-tool", "Edit|Write|MultiEdit|NotebookEdit"),
    ("Stop", "stop", None),
]


def _default_settings_path() -> Path:
    return Path(os.path.expanduser("~/.claude/settings.json"))


def _managed_entry(mode: str, matcher: str | None) -> dict:
    entry: dict = {
        "type": "command",
        "command": f"{HOOK_BIN} {mode}",
        MARKER: SHIM_VERSION,
    }
    block: dict = {"hooks": [entry]}
    if matcher is not None:
        block["matcher"] = matcher
    return block


def _strip_managed(hooks: dict) -> dict:
    """Return a copy of the hooks mapping with all MARKER-tagged entries removed."""
    out: dict = {}
    for event, blocks in hooks.items():
        kept_blocks = []
        for block in blocks:
            kept_hooks = [h for h in block.get("hooks", []) if MARKER not in h]
            if kept_hooks:
                nb = dict(block)
                nb["hooks"] = kept_hooks
                kept_blocks.append(nb)
        if kept_blocks:
            out[event] = kept_blocks
    return out


def _load(settings: Path) -> dict:
    if settings.exists() and settings.read_text().strip():
        return json.loads(settings.read_text())
    return {}


def _backup(settings: Path) -> None:
    if settings.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings, settings.with_suffix(settings.suffix + f".kb-bak-{ts}"))


def cmd_install(settings: Path) -> int:
    data = _load(settings)
    _backup(settings)
    hooks = _strip_managed(data.get("hooks", {}))  # remove any prior managed block first
    for event, mode, matcher in HOOK_EVENTS:
        hooks.setdefault(event, []).append(_managed_entry(mode, matcher))
    data["hooks"] = hooks
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n")
    print(f"installed data-olympus enforcement (v{SHIM_VERSION}) into {settings}")
    return 0


def cmd_uninstall(settings: Path) -> int:
    data = _load(settings)
    if "hooks" not in data:
        print("nothing to uninstall")
        return 0
    _backup(settings)
    data["hooks"] = _strip_managed(data["hooks"])
    if not data["hooks"]:
        del data["hooks"]
    settings.write_text(json.dumps(data, indent=2) + "\n")
    print(f"uninstalled data-olympus enforcement from {settings}")
    return 0


def cmd_status(settings: Path) -> int:
    data = _load(settings)
    versions = {
        h[MARKER]
        for blocks in data.get("hooks", {}).values()
        for block in blocks
        for h in block.get("hooks", [])
        if MARKER in h
    }
    if not versions:
        print("claude-code: not installed")
        return 0
    stale = " (stale; run `kb enforce install`)" if SHIM_VERSION not in versions else ""
    print(f"claude-code: installed, tier=hard, versions={sorted(versions)}{stale}")
    return 0


def cmd_doctor(settings: Path) -> int:
    endpoint = os.getenv("KB_ENDPOINT", "http://localhost:8080")
    try:
        with urllib.request.urlopen(f"{endpoint}/api/v1/health", timeout=5) as r:
            ok = r.status == 200
    except Exception as exc:  # noqa: BLE001 - report any failure
        print(f"doctor: cannot reach {endpoint}: {exc}")
        return 1
    print(f"doctor: endpoint {endpoint} reachable={ok}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="kb enforce")
    p.add_argument("command", choices=["install", "uninstall", "status", "doctor"])
    p.add_argument("--agent", default="claude-code")
    p.add_argument("--settings", default=None)
    args = p.parse_args(argv)
    if args.agent != "claude-code":
        print(f"kb enforce: provider '{args.agent}' not implemented in slice 1", file=sys.stderr)
        return 64
    settings = Path(args.settings) if args.settings else _default_settings_path()
    return {
        "install": cmd_install, "uninstall": cmd_uninstall,
        "status": cmd_status, "doctor": cmd_doctor,
    }[args.command](settings)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

Make it executable:

```bash
chmod +x bin/_kb_enforce.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kb_enforce_install.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/_kb_enforce.py tests/test_kb_enforce_install.py
git commit -m "feat(enforce): add kb enforce installer (Claude Code provider)"
```

---

### Task 12: `kb enforce` subcommand in `bin/kb`

**Files:**
- Modify: `bin/kb` (add `enforce` dispatch + usage line)
- Test: `tests/test_kb_cli_enforce.bats`

- [ ] **Step 1: Write the failing test**

```bash
# tests/test_kb_cli_enforce.bats
#!/usr/bin/env bats
# bats tests for `kb enforce` subcommand.

setup_file() {
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export KB="${REPO_ROOT}/bin/kb"
}

@test "kb enforce status on empty settings reports not installed" {
  TMP="$(mktemp -d)"
  echo "{}" > "$TMP/settings.json"
  run "$KB" enforce status --settings "$TMP/settings.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"not installed"* ]]
  rm -rf "$TMP"
}

@test "kb enforce install then status reports installed" {
  TMP="$(mktemp -d)"
  echo "{}" > "$TMP/settings.json"
  run "$KB" enforce install --settings "$TMP/settings.json"
  [ "$status" -eq 0 ]
  run "$KB" enforce status --settings "$TMP/settings.json"
  [[ "$output" == *"installed"* ]]
  rm -rf "$TMP"
}

@test "kb enforce with no subcommand is a usage error" {
  run "$KB" enforce
  [ "$status" -eq 64 ]
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bats tests/test_kb_cli_enforce.bats`
Expected: FAIL (`kb enforce` falls through to `usage`, exit 64 — but install/status tests fail)

- [ ] **Step 3: Add the dispatch to `bin/kb`**

In `bin/kb`, add an `enforce` case to the early dispatch block (the `case "$SUBCMD" in` near line 555, alongside `propose`/`resolve`/`audit`):

```bash
  enforce)
    [[ $# -ge 1 ]] || { echo "kb: enforce requires subcommand (install|uninstall|status|doctor)" >&2; exit 64; }
    exec python3 "$(dirname "$0")/_kb_enforce.py" "$@"
    ;;
```

Add a usage line in the header comment block (after the `kb onboard rename` line, before the blank line that terminates the `Usage:` sed range):

```bash
#   kb enforce install|uninstall|status|doctor [--agent NAME] [--settings PATH]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bats tests/test_kb_cli_enforce.bats`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/kb tests/test_kb_cli_enforce.bats
git commit -m "feat(enforce): wire kb enforce subcommand to the installer"
```

---

### Task 13: Docs, CHANGELOG, full suite, lint

**Files:**
- Create: `docs/enforcement.md`
- Modify: `SPEC.md` (serving-contracts section: list the new endpoints)
- Modify: `CHANGELOG.md` (`[Unreleased]` block — MANDATORY)
- Modify: `README.md` (add enforcement.md to the Documentation list)

- [ ] **Step 1: Write `docs/enforcement.md`**

```markdown
# Enforcement (mandatory consultation gate)

data-olympus can act as an enforced gate for code and architectural decisions,
not only an advisory knowledge base. Enforcement is per-agent: it runs in each
agent's hook surface, driven by a shared policy core in the server.

## Server endpoints

- `POST /api/v1/consult` — record a consultation for `(source_session, workspace)`
  and return the governing rules for an intent. Body:
  - `workspace`
  - `intent`
  - `source_session`
  - `agent_identity`
- `POST /api/v1/gate/check` — verdict (`allow` | `consult_required`) for a pending
  code action. Body:
  - `workspace`
  - `session_id`
  - `tool_name`
  - `action_path`
  - `action_diff` (optional)
- `GET /api/v1/compliance` — aggregated enforcement-event counts.

The same three are exposed as the `kb_consult`, `kb_gate_check`, and
`kb_compliance` MCP tools.

## Configuration

- `KB_CONSULT_TTL_SEC` (default 300): how long a consultation stays fresh.
- `KB_ENFORCE_FAIL_MODE` (default `open`): hook behaviour when the server is
  unreachable. `open` allows the action with a warning; `closed` blocks it.

## Installing the Claude Code gate

```bash
kb enforce install --agent claude-code   # idempotent; backs up settings first
kb enforce status                        # show install state, tier, version
kb enforce doctor                        # verify the wiring reaches the server
kb enforce uninstall --agent claude-code # surgical removal of the managed block
```

The installer writes a managed hook block (SessionStart, UserPromptSubmit,
PreToolUse, Stop) into `~/.claude/settings.json`, tagged so re-runs never
duplicate entries and uninstall never touches operator-authored settings.

## Per-agent ceiling

Hard block is available for Claude Code (and, in a later slice, OpenCode). Other
agents degrade to soft inject + audit. See the design spec for the full table.
```

- [ ] **Step 2: Add the endpoints to `SPEC.md`**

Find the serving-contracts section listing `/api/v1/*` endpoints and add the three enforcement endpoints (`/consult`, `/gate/check`, `/compliance`) to that list, matching the surrounding formatting.

- [ ] **Step 3: Update `CHANGELOG.md` (mandatory)**

Under the topmost `## [Unreleased]` block, add to the `### Added` list:

```markdown
- Enforcement core: data-olympus can now act as a gated, mandatory consultation
  proxy for code/architectural decisions, not only an advisory KB. New MCP tools
  `kb_consult`, `kb_gate_check`, `kb_compliance` and REST endpoints
  `/api/v1/consult`, `/api/v1/gate/check`, `/api/v1/compliance`, backed by a
  shared heuristic intent classifier and an in-memory consultation ledger.
- `kb enforce install|uninstall|status|doctor` CLI plus a `kb-enforce-hook`
  dispatcher install an idempotent, reversible Claude Code enforcement shim
  (SessionStart / UserPromptSubmit / PreToolUse / Stop). Fail-open by default
  (`KB_ENFORCE_FAIL_MODE`), consultation freshness via `KB_CONSULT_TTL_SEC`.
```

- [ ] **Step 4: Update `README.md`**

In the `## Documentation` list, add:

```markdown
- [`docs/enforcement.md`](docs/enforcement.md): turning the KB into a mandatory consultation gate (hooks, `kb enforce`).
```

- [ ] **Step 5: Run the full suite + lint**

Run:
```bash
uv run ruff check .
uv run pytest
bats tests/test_kb_enforce_hook.bats tests/test_kb_cli_enforce.bats
uv run data-olympus lint example-bundle
```
Expected: ruff clean, all pytest green, bats green, lint exits 0.

- [ ] **Step 6: Commit**

```bash
git add docs/enforcement.md SPEC.md CHANGELOG.md README.md
git commit -m "docs(enforce): document enforcement gate, endpoints, and kb enforce CLI"
```

---

## Self-Review

**Spec coverage:**
- Consultation receipt/ledger → Task 3, 4 (ledger + `kb_consult_fn`).
- `policy/check` + intent classifier → Task 2, 5 (`IntentClassifier`, `kb_gate_check_fn`).
- Compliance audit (new event types `consult`/`gate_allow`/`gate_block`/`gate_bypass`/`gate_degraded`) → Tasks 4, 5, 6 emit/aggregate them; `gate_bypass` is defined in `ENFORCE_EVENT_TYPES` for soft-tier agents (slice 2) and `gate_degraded` is emitted by the hook’s fail-open path (Task 10).
- Claude Code reference shim (SessionStart/UserPromptSubmit/PreToolUse/Stop) → Task 10.
- `kb enforce install/uninstall/status/doctor` + provider interface → Tasks 11, 12 (provider gate: non-claude-code agents return exit 64, the slice boundary).
- Config (`KB_CONSULT_TTL_SEC`, `KB_ENFORCE_FAIL_MODE`) → Task 7 (server TTL) + Task 10 (hook fail-mode).
- Fail-open default → Task 10 tests (`fails open` and `fails closed` cases).
- Changelog mandate → Task 13 Step 3.

**Out-of-scope items honored:** no providers other than Claude Code (Task 11 returns 64 for others); no egress proxy; heuristic classifier only (pluggable interface in Task 2); ledger not persisted; no per-intent binding.

**Placeholder scan:** every code/test step contains complete code; no TBD/TODO; commands have expected output.

**Type/name consistency:** `kb_consult_fn`, `kb_gate_check_fn`, `kb_compliance_fn`, `IntentClassifier.classify`, `ConsultationLedger.record/is_fresh/get`, `ConsultResponse/GateCheckResponse/ComplianceResponse`, `ENFORCE_EVENT_TYPES`, `MARKER`/`SHIM_VERSION`, and `bin/kb-enforce-hook` modes (`session-start|user-prompt|pre-tool|stop`) are used identically across every task that references them. The hook helper `json_field_from` is defined in Task 10 Step 3 and used in the same file.

**Known follow-ups (not blockers for slice 1):** the planned CI gate that fails a functional PR without a `[Unreleased]` changelog edit (tracked in `.rules/changelog-per-release.md`); slice 2 (per-agent providers) and slice 3 (egress proxy).
