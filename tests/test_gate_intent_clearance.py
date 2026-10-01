"""Issue #296: a consult clears only the governed actions its intent covers.

The gate used to clear on any fresh explicit consult for the
``(session_id, workspace)`` pair, so a consult about one topic let an unrelated
governed action through inside the TTL. Under ``KB_GATE_CLEARANCE=intent`` (the
default) each explicit consult records a coverage set computed from its intent,
and a governed action is allowed only when every one of its signals is covered,
directly or through the fixed family mapping in ``enforce_policy``.
``KB_GATE_CLEARANCE=pair`` keeps the previous rule.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

from data_olympus.audit_log import AuditLog
from data_olympus.enforce_policy import (
    GATE_CLEARANCE_PAIR,
    GOVERNED_COMMAND_PATTERNS,
    GOVERNED_PATH_GLOBS,
    PROMPT_HOOK_TRIGGER,
    ConsultationLedger,
    IntentClassifier,
)
from data_olympus.tools_enforce import kb_consult_fn, kb_gate_check_fn

if TYPE_CHECKING:
    from pathlib import Path

    from data_olympus.models import GateCheckResponse


class _FakeIndex:
    def search(self, query, limit=20, tier=None, category=None, status=None,  # noqa: ARG002
               in_force=False, doc_type=None, **kwargs):  # noqa: ARG002
        return []

    def health(self):
        return {"source_commit": "deadbeef"}


def _consult(
    led: ConsultationLedger, intent: str, *, now: float = 1000.0,
    classifier: IntentClassifier | None = None, trigger: str = "explicit",
) -> None:
    kb_consult_fn(
        idx=_FakeIndex(), classifier=classifier or IntentClassifier(), ledger=led,
        workspace="proj", intent=intent, source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=now, trigger=trigger,
    )


def _gate(
    led: ConsultationLedger, *, path: str | None = None, diff: str = "",
    now: float = 1100.0, classifier: IntentClassifier | None = None,
    clearance: str = "intent", audit_log: AuditLog | None = None,
) -> GateCheckResponse:
    return kb_gate_check_fn(
        classifier=classifier or IntentClassifier(), ledger=led,
        workspace="proj", session_id="s1",
        tool_name="Bash" if path is None else "Edit",
        action_path=path, action_diff=diff, now=now, ttl_sec=300.0,
        audit_log=audit_log, clearance=clearance,
    )


def _suggested_intent(reason: str) -> str:
    m = re.search(r"intent='(.*)'\) then retry", reason)
    assert m is not None, reason
    return m.group(1)


# --- check 1: a mismatched consult does not clear -----------------------------


def test_schema_consult_does_not_clear_a_pip_install(tmp_path: Path) -> None:
    led = ConsultationLedger()
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    _consult(led, "change the schema")
    resp = _gate(led, diff="pip install requests", audit_log=al)
    assert resp.verdict == "consult_required"
    assert "command:pip install" in resp.reason
    block = [e for e in al.iter_filtered() if e["event_type"] == "gate_block"]
    assert len(block) == 1
    assert block[0]["status"] == "consult_required"
    assert block[0]["uncovered"] == ["command:pip install"]


# --- check 2: the family mapping clears ordinary topical consults ------------


@pytest.mark.parametrize(
    ("path", "diff"),
    [("/p/pyproject.toml", ""), (None, "uv add requests")],
)
def test_dependency_consult_clears_manifest_edit_and_install(
    path: str | None, diff: str,
) -> None:
    led = ConsultationLedger()
    _consult(led, "add a dependency")
    assert _gate(led, path=path, diff=diff).verdict == "allow"


# --- check 3: families, and the Dockerfile family has no keyword ------------


def test_dockerfile_consult_clears_dockerfile_and_dependency_consult_does_not() -> None:
    named = ConsultationLedger()
    _consult(named, "update the Dockerfile base image")
    assert _gate(named, path="/p/Dockerfile").verdict == "allow"

    topical = ConsultationLedger()
    _consult(topical, "add a dependency")
    resp = _gate(topical, path="/p/Dockerfile")
    assert resp.verdict == "consult_required"
    assert "path:Dockerfile" in resp.reason


@pytest.mark.parametrize(
    "path", ["services/api/go.mod", "app/requirements-dev.txt", "java/pom.xml"],
)
def test_dependency_consult_clears_nested_manifests_by_family(path: str) -> None:
    led = ConsultationLedger()
    _consult(led, "add a dependency")
    assert _gate(led, path=path).verdict == "allow"


# --- check 3a: basename consults --------------------------------------------


def test_basename_consult_clears_a_nested_manifest_with_a_bare_twin() -> None:
    led = ConsultationLedger()
    _consult(led, "bump the version in Cargo.toml")
    assert _gate(led, path="crates/x/Cargo.toml").verdict == "allow"


def test_pom_basename_alone_does_not_clear_and_the_denial_names_the_path() -> None:
    led = ConsultationLedger()
    _consult(led, "edit pom.xml")
    resp = _gate(led, path="java/pom.xml")
    assert resp.verdict == "consult_required"
    assert "java/pom.xml" in resp.reason

    with_keyword = ConsultationLedger()
    _consult(with_keyword, "edit pom.xml to bump a dependency")
    assert _gate(with_keyword, path="java/pom.xml").verdict == "allow"


# --- check 3b: an action spanning two families ------------------------------


def test_install_with_a_migration_keyword_needs_both_and_the_suggestion_clears() -> None:
    led = ConsultationLedger()
    _consult(led, "add a dependency")
    resp = _gate(led, diff="pip install django-migration")
    assert resp.verdict == "consult_required"
    assert "keyword:migration" in resp.reason
    # The dependency consult already covers the install itself.
    assert "pip install" not in resp.reason

    _consult(led, _suggested_intent(resp.reason), now=1050.0)
    assert _gate(led, diff="pip install django-migration").verdict == "allow"


def test_suggested_intent_alone_clears_every_uncovered_signal() -> None:
    """The denial suggests ONE intent covering all uncovered signals at once."""
    first = ConsultationLedger()
    resp = _gate(first, path="java/pom.xml", diff="run pip install x")
    assert resp.verdict == "consult_required"
    assert "path:*/pom.xml" in resp.reason
    assert "command:pip install" in resp.reason

    fresh = ConsultationLedger()
    _consult(fresh, _suggested_intent(resp.reason))
    assert _gate(fresh, path="java/pom.xml", diff="run pip install x").verdict == "allow"


# --- check 4: several fresh consults combine ---------------------------------


def test_two_fresh_consults_with_different_topics_clear_an_action_needing_both() -> None:
    led = ConsultationLedger()
    _consult(led, "add a dependency", now=1000.0)
    assert _gate(led, diff="pip install django-migration").verdict == "consult_required"
    _consult(led, "plan a migration", now=1010.0)
    assert _gate(led, diff="pip install django-migration").verdict == "allow"


# --- check 5: each signal expires on its own TTL -----------------------------


def test_a_stale_signal_stops_covering_while_a_newer_one_still_does() -> None:
    led = ConsultationLedger()
    _consult(led, "plan a migration", now=1000.0)
    _consult(led, "add a dependency", now=1250.0)
    # At t=1350 the migration consult is 350 s old (stale), the dependency one
    # 100 s old (fresh).
    assert _gate(led, path="db/migrations/0002.py", now=1350.0).verdict == "consult_required"
    assert _gate(led, path="pyproject.toml", now=1350.0).verdict == "allow"


# --- check 6: no deadlock ------------------------------------------------------

# One action path per built-in glob, chosen so the classifier reaches that glob
# (or, where an earlier glob always wins, the glob that actually fires).
_GLOB_EXAMPLES = {
    "pyproject.toml": "pyproject.toml",
    "*/pyproject.toml": "svc/pyproject.toml",
    "package.json": "package.json",
    "*/package.json": "web/package.json",
    "*/requirements*.txt": "app/requirements-dev.txt",
    "requirements*.txt": "requirements.txt",
    "*/go.mod": "services/api/go.mod",
    "go.mod": "go.mod",
    "*/Cargo.toml": "crates/x/Cargo.toml",
    "Cargo.toml": "Cargo.toml",
    "*/pom.xml": "java/pom.xml",
    "*/migrations/*": "db/migrations/0001_init.py",
    "*/migration/*": "db/migration/v1.py",
    "*/schema/*": "db/schema/users.yaml",
    "*/schema.sql": "db/schema.sql",
    "*.sql": "seed.sql",
    "Dockerfile": "Dockerfile",
    "*/Dockerfile": "ops/Dockerfile",
    "*/docker-compose*.yml": "ops/docker-compose.dev.yml",
    "docker-compose*.yml": "docker-compose.yml",
}


def test_glob_examples_cover_every_built_in_glob() -> None:
    assert set(_GLOB_EXAMPLES) == set(GOVERNED_PATH_GLOBS)


@pytest.mark.parametrize("prefix", ["", "/work/repo/"])
@pytest.mark.parametrize("glob", GOVERNED_PATH_GLOBS)
def test_naming_the_full_path_clears_every_built_in_glob(glob: str, prefix: str) -> None:
    path = prefix + _GLOB_EXAMPLES[glob]
    led = ConsultationLedger()
    assert _gate(led, path=path).verdict == "consult_required"
    _consult(led, f"edit {path}")
    assert _gate(led, path=path).verdict == "allow"


@pytest.mark.parametrize("pattern", GOVERNED_COMMAND_PATTERNS)
def test_naming_the_command_fragment_clears_every_built_in_pattern(pattern: str) -> None:
    command = f"{pattern}example"
    led = ConsultationLedger()
    assert _gate(led, diff=command).verdict == "consult_required"
    _consult(led, f"run {command}")
    assert _gate(led, diff=command).verdict == "allow"


@pytest.mark.parametrize(
    ("path", "diff", "intent"),
    [
        (None, "run a window test now", "run a window test"),
        ("desktop/session.md", "", "edit desktop/session.md"),
        (None, "xdotool key Return", "use xdotool key Return"),
    ],
)
def test_naming_a_configured_extra_clears_it(
    path: str | None, diff: str, intent: str,
) -> None:
    classifier = IntentClassifier(
        keywords=("window test",), path_globs=("desktop/**",),
        command_patterns=("xdotool",),
    )
    led = ConsultationLedger()
    assert _gate(led, path=path, diff=diff, classifier=classifier).verdict == (
        "consult_required"
    )
    _consult(led, intent, classifier=classifier)
    assert _gate(led, path=path, diff=diff, classifier=classifier).verdict == "allow"


def test_a_quoted_path_with_spaces_is_one_token() -> None:
    led = ConsultationLedger()
    path = "my repo/desktop/session.md"
    classifier = IntentClassifier(path_globs=("my repo/desktop/*",))
    _consult(led, f'edit "{path}"', classifier=classifier)
    assert _gate(led, path=path, classifier=classifier).verdict == "allow"


# --- check 7: prompt-hook consults never add coverage ------------------------


def test_prompt_hook_consult_adds_no_coverage() -> None:
    led = ConsultationLedger()
    _consult(led, "add a dependency", trigger=PROMPT_HOOK_TRIGGER)
    assert _gate(led, path="pyproject.toml").verdict == "consult_required"


def test_prompt_hook_consult_does_not_drop_explicit_coverage() -> None:
    led = ConsultationLedger()
    _consult(led, "add a dependency", now=1000.0)
    _consult(led, "say hello", now=1010.0, trigger=PROMPT_HOOK_TRIGGER)
    assert _gate(led, path="pyproject.toml").verdict == "allow"


# --- check 8: a legacy ledger ---------------------------------------------------


def test_legacy_ledger_clears_nothing_under_intent_and_as_before_under_pair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps([
        {"session_id": "s1", "workspace": "proj", "consulted_at": 1000.0,
         "rule_ids": [], "explicit_at": 1000.0},
    ]), encoding="utf-8")
    led = ConsultationLedger(path=str(path))
    assert led.get(session_id="s1", workspace="proj") is not None
    assert _gate(led, path="pyproject.toml").verdict == "consult_required"
    assert _gate(led, path="pyproject.toml", clearance=GATE_CLEARANCE_PAIR).verdict == "allow"


def test_coverage_survives_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "ledger.json")
    _consult(ConsultationLedger(path=path), "add a dependency")
    reloaded = ConsultationLedger(path=path)
    assert _gate(reloaded, path="pyproject.toml").verdict == "allow"
    assert _gate(reloaded, path="Dockerfile").verdict == "consult_required"


def test_eviction_drops_signals_older_than_retention() -> None:
    led = ConsultationLedger(retention_sec=300.0)
    led.record(session_id="s1", workspace="proj", rule_ids=[], now=1000.0,
               signals=["keyword:migration"])
    led.record(session_id="s1", workspace="proj", rule_ids=[], now=1400.0,
               signals=["keyword:dependency"])
    entry = led.get(session_id="s1", workspace="proj")
    assert entry is not None
    assert entry.explicit_signals == {"keyword:dependency": 1400.0}


# --- check 9: pair mode ------------------------------------------------------


def test_pair_mode_clears_on_any_fresh_explicit_consult() -> None:
    led = ConsultationLedger()
    _consult(led, "say hello")
    assert _gate(led, diff="pip install requests",
                 clearance=GATE_CLEARANCE_PAIR).verdict == "allow"
    assert _gate(led, diff="pip install requests").verdict == "consult_required"
