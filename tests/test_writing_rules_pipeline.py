"""Writing rules through the real write pipeline (#283).

The scan itself is covered in test_writing_rules.py. These tests drive the
propose, resolve and multifile paths against real git worktrees, and the REST
status mappers, config loading and production wiring around them.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from data_olympus import tools_write
from data_olympus.audit_log import AuditLog
from data_olympus.config import load_config
from data_olympus.rest_api import _propose_status, _resolve_status
from data_olympus.scaffold import scaffold_bundle
from data_olympus.tools_write import (
    _WriteRejected,
    commit_multifile_in_worktree,
    kb_propose_edit_fn,
    kb_propose_memory_fn,
    kb_resolve_pending_fn,
)
from data_olympus.writing_rules import WritingRulesPolicy, added_line_findings
from tests.test_tools_write import _build_index, _seed_t1_file, _set_git_env, _state

DASH = "—"
ENFORCE = WritingRulesPolicy(mode="enforce")
WARN = WritingRulesPolicy(mode="warn")
OFF = WritingRulesPolicy(mode="off")
SEED = "---\nid: STD-U-001\ntier: T1\n---\n# T1\nbody\n"


def _edit(tmp_path, monkeypatch, postimage, policy, *, seed=SEED, audit=None,
          agent_identity="claude"):
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    if seed != SEED:
        (repo / target).write_text(seed, encoding="utf-8")
        import subprocess
        subprocess.run(["git", "-C", str(repo), "commit", "-qam", "reseed"], check=True)
        blob = subprocess.check_output(
            ["git", "-C", str(repo), "ls-tree", "HEAD", target], text=True).split()[2]
    resp = kb_propose_edit_fn(
        target_path=target, postimage=postimage, base_commit="HEAD",
        base_blob_sha=blob, target_file_hash=None, reason="edit",
        source_session="s", agent_identity=agent_identity, confidence=0.9,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        idx=_build_index(repo), audit_log=audit, writing_rules=policy,
    )
    return resp, pq


def _audit_rows(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# 1 / 2 ---------------------------------------------------------------------

def test_an_edit_that_keeps_an_old_offending_line_and_adds_clean_text_commits(
    tmp_path, monkeypatch,
) -> None:
    seed = SEED + f"old {DASH} line\n"
    resp, pq = _edit(tmp_path, monkeypatch, seed + "a clean addition\n", ENFORCE,
                     seed=seed)
    assert resp.status == "committed", resp
    assert resp.writing_rule_findings is None


def test_an_added_offending_line_is_rejected_with_line_and_rule(
    tmp_path, monkeypatch,
) -> None:
    resp, pq = _edit(tmp_path, monkeypatch, SEED + f"new {DASH} line\n", ENFORCE)
    assert resp.status == "rejected_writing_rule"
    assert resp.writing_rule_findings and resp.writing_rule_findings[0].startswith(
        "line 7: em-dash:")
    assert pq.size() == 0  # nothing committed


# 7 / 11a -------------------------------------------------------------------

@pytest.mark.parametrize("identity", ["claude", "data-olympus-system"])
def test_a_new_memory_with_an_offending_line_is_rejected_whoever_claims_to_write(
    tmp_path, monkeypatch, identity,
) -> None:
    # The maintenance exemption is keyed on the call path, never on identity:
    # a client claiming the system identity is still gated.
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text=f"a memory {DASH} with a dash", tags=[], source_session="s",
        agent_identity=identity, confidence=0.9, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl,
        blocklist=bl, remote_addr="1.2.3.4", writing_rules=ENFORCE,
    )
    assert resp.status == "rejected_writing_rule"
    assert pq.size() == 0


def test_an_edit_claiming_the_system_identity_is_still_gated(
    tmp_path, monkeypatch,
) -> None:
    resp, _ = _edit(tmp_path, monkeypatch, SEED + f"x {DASH} y\n", ENFORCE,
                    agent_identity="data-olympus-system")
    assert resp.status == "rejected_writing_rule"


# 8 -------------------------------------------------------------------------

def test_a_postimage_with_a_secret_and_a_dash_is_reported_as_the_secret(
    tmp_path, monkeypatch,
) -> None:
    resp, _ = _edit(tmp_path, monkeypatch,
                    SEED + f"key AKIAIOSFODNN7EXAMPLE {DASH} here\n", ENFORCE)
    assert resp.status == "rejected_secret_detected"
    assert resp.writing_rule_findings is None
    assert "AKIA" not in (resp.reason or "")


# 9 -------------------------------------------------------------------------

def test_a_resolve_whose_edited_text_adds_an_offending_line_is_refused_and_restored(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    parked = kb_propose_memory_fn(
        text="a clean memory", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=parked.pending_id, decision="edit",
        edited_text=f"an edited memory {DASH} with a dash",
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator", writing_rules=ENFORCE,
    )
    assert resp.status == "rejected_writing_rule"
    assert resp.writing_rule_findings
    assert _resolve_status(resp.status) == 422
    assert pen.size() == 1  # restored for the operator to re-resolve
    assert pq.size() == 0


# 10 / 11 -------------------------------------------------------------------

def _bundle_args(tmp_path):
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    from data_olympus.tools_write import _DEFAULT_SERIALIZER
    return git, dict(worktrees=reg, push_queue=pq, pending=pen,
                     serializer=_DEFAULT_SERIALIZER, idx=None, subject="bundle",
                     target_tier="T3", target_path_for_msg="projects/x/",
                     confidence=1.0), pq


def test_one_offending_file_rejects_the_whole_bundle(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, args, pq = _bundle_args(tmp_path)
    files = [{"target_path": "projects/x/a.md", "postimage": "# A\n\nclean\n"},
             {"target_path": "projects/x/b.md", "postimage": f"# B\n\nx {DASH} y\n"}]
    with pytest.raises(_WriteRejected) as rej:
        commit_multifile_in_worktree(source_session="b", agent_identity="claude",
                                     files=files, writing_rules=ENFORCE, **args)
    assert rej.value.response.status == "rejected_writing_rule"
    assert rej.value.response.target_path == "projects/x/b.md"
    assert pq.size() == 0


def test_the_machine_rendered_exemption_commits_what_enforce_would_reject(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    git, args, pq = _bundle_args(tmp_path)
    files = [{"target_path": "projects/x/ledger.md",
              "postimage": ("---\nid: x-ledger\nstatus: active\n---\n# Ledger\n\n"
                            f"id std-u-001{DASH}no-sugarcoating\n")}]
    sha, _ = commit_multifile_in_worktree(
        source_session="system:maintenance-ledger",
        agent_identity="data-olympus-system", files=files,
        writing_rules=ENFORCE, writing_rule_exempt=True, **args)
    assert sha


def test_the_exemption_is_a_tripwire_for_any_other_identity(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    git, args, pq = _bundle_args(tmp_path)
    with pytest.raises(AssertionError):
        commit_multifile_in_worktree(
            source_session="b", agent_identity="claude",
            files=[{"target_path": "projects/x/a.md", "postimage": "# A\n"}],
            writing_rules=ENFORCE, writing_rule_exempt=True, **args)


def test_the_exemption_is_on_no_client_surface() -> None:
    import inspect

    for fn in (kb_propose_edit_fn, kb_propose_memory_fn, kb_resolve_pending_fn):
        assert "writing_rule_exempt" not in inspect.signature(fn).parameters
    maintenance = (Path(tools_write.__file__).parent / "maintenance.py").read_text()
    assert "writing_rule_exempt=True" in maintenance
    callers = [p for p in Path(tools_write.__file__).parent.rglob("*.py")
               if "writing_rule_exempt=True" in p.read_text()]
    assert [p.name for p in callers] == ["maintenance.py"]


# 12 ------------------------------------------------------------------------

def test_the_scaffold_templates_are_clean_under_the_raw_rules(tmp_path) -> None:
    scaffold_bundle(tmp_path / "bundle")
    docs = list((tmp_path / "bundle").rglob("*.md"))
    assert docs
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        assert added_line_findings(preimage="", postimage=text) == [], doc


# 13 / 14 -------------------------------------------------------------------

def test_warn_mode_commits_and_reports_in_the_response_audit_and_log(
    tmp_path, monkeypatch, caplog,
) -> None:
    audit = AuditLog(log_path=str(tmp_path / "audit.log"))
    with caplog.at_level(logging.WARNING, logger="data_olympus.tools_write"):
        resp, pq = _edit(tmp_path, monkeypatch, SEED + f"x {DASH} y\n", WARN,
                         audit=audit)
    assert resp.status == "committed"
    assert resp.writing_rule_findings and "em-dash" in resp.writing_rule_findings[0]
    rows = _audit_rows(tmp_path / "audit.log")
    assert any('"writing_rules": "warned:em-dash@7"' in r for r in rows), rows
    assert not any(DASH in r for r in rows), "the audit log must not carry excerpts"
    assert any("writing-rule findings" in r.getMessage() for r in caplog.records)
    # The log carries the count only: no path, no excerpt (CodeQL
    # py/clear-text-logging-sensitive-data; the audit event holds the path).
    messages = [r.getMessage() for r in caplog.records]
    assert not any("STD-U-001" in m or DASH in m for m in messages), messages


def test_off_mode_never_computes_the_diff(tmp_path, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(tools_write, "added_line_findings",
                        lambda **_k: calls.append(1) or [])
    resp, _ = _edit(tmp_path, monkeypatch, SEED + f"x {DASH} y\n", OFF)
    assert resp.status == "committed"
    assert calls == []


# 15 ------------------------------------------------------------------------

def test_an_excluded_path_is_not_scanned(tmp_path, monkeypatch) -> None:
    policy = WritingRulesPolicy(mode="enforce", exclude_paths=("universal/*",))
    resp, _ = _edit(tmp_path, monkeypatch, SEED + f"x {DASH} y\n", policy)
    assert resp.status == "committed"


# 20 / 22 -------------------------------------------------------------------

def _boom(**_k):
    raise RuntimeError("regex pathology")


def test_a_failing_scan_rejects_in_enforce(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tools_write, "added_line_findings", _boom)
    resp, pq = _edit(tmp_path, monkeypatch, SEED + "clean\n", ENFORCE)
    assert resp.status == "rejected_writing_rule"
    assert "writing-rule check failed" in (resp.reason or "")
    assert pq.size() == 0


def test_a_failing_scan_warns_and_commits_in_warn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tools_write, "added_line_findings", _boom)
    resp, _ = _edit(tmp_path, monkeypatch, SEED + "clean\n", WARN)
    assert resp.status == "committed"
    assert resp.writing_rule_findings and "check failed" in resp.writing_rule_findings[0]


def test_an_undecodable_existing_target_is_rejected_not_treated_as_empty(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    # The target is unreadable as UTF-8 by design, so no index can be built
    # over it; with no index the governed-lane check would park the edit
    # before the gate under test runs.
    monkeypatch.setenv("KB_GOVERNED_LANE_PROTECTION", "off")
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target = "universal/foundation/latin1.md"
    (repo / target).parent.mkdir(parents=True, exist_ok=True)
    (repo / target).write_bytes(b"---\nid: L1\n---\ncaf\xe9\n")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "latin1"], check=True)
    resp = kb_propose_edit_fn(
        target_path=target, postimage="---\nid: L1\n---\ncafe\n", base_commit=None,
        base_blob_sha=None, target_file_hash=None, reason="fix", source_session="s",
        agent_identity="claude", confidence=0.9, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4", idx=None, writing_rules=ENFORCE,
    )
    assert resp.status == "rejected_writing_rule"
    assert "UnicodeDecodeError" in (resp.reason or "")


# 17 ------------------------------------------------------------------------

# Every status a write tool can return, with the HTTP code each mapper gives it.
# A new status fails here until it is mapped on purpose (issue #283, closing
# the class #291 exposed rather than one instance).
_EXPECTED = {
    "committed": (201, 200),
    "pending_confirmation": (202, 200),
    "already_resolved": (400, 409),
    "rejected": (400, 200),
    "rejected_already_in_progress": (409, 200),
    "rejected_already_onboarded": (400, 200),
    "rejected_bad_decision": (400, 400),
    "rejected_contest_index_unavailable": (503, 200),
    "rejected_edited_text_too_large": (400, 413),
    "rejected_empty_bundle": (400, 200),
    "rejected_invalid_base": (400, 200),
    "rejected_invalid_contest": (400, 200),
    "rejected_invalid_document": (422, 422),
    "rejected_invalid_encoding": (400, 400),
    "rejected_invalid_evidence": (400, 200),
    "rejected_path_blocked": (400, 200),
    "rejected_path_lock_busy": (409, 200),
    "rejected_path_locked": (423, 200),
    "rejected_path_not_indexable": (400, 200),
    "rejected_path_not_indexable_or_blocked": (400, 200),
    "rejected_payload_too_large": (413, 200),
    "rejected_pending_queue_full": (429, 200),
    "rejected_rate_limited": (429, 200),
    "rejected_secret_detected": (422, 422),
    "rejected_stale_base": (409, 409),
    "rejected_symlink_escape": (400, 400),
    "rejected_too_many_files": (413, 200),
    "rejected_writing_rule": (422, 422),
}


def test_every_write_status_is_mapped_on_purpose() -> None:
    src_dir = Path(tools_write.__file__).parent
    source = "".join((src_dir / f).read_text()
                     for f in ("tools_write.py", "tools_onboarding.py"))
    found = set(re.findall(
        r'status="((?:rejected_[a-z_]+)|committed|pending_confirmation'
        r'|already_resolved|rejected)"', source))
    assert found == set(_EXPECTED), sorted(found ^ set(_EXPECTED))
    for status, (propose, resolve) in _EXPECTED.items():
        assert (_propose_status(status), _resolve_status(status)) == (propose, resolve), status


# 21 ------------------------------------------------------------------------

def test_an_invalid_mode_fails_config_load_naming_the_accepted_values(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KB_WRITING_RULES_MODE", "enfoce")
    with pytest.raises(ValueError, match="enforce, warn, off"):
        load_config()


def test_the_mode_and_exclusions_load_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("KB_WRITING_RULES_MODE", "enforce")
    monkeypatch.setenv("KB_WRITING_RULES_EXCLUDE_PATHS", "archive/*, *.txt")
    cfg = load_config()
    assert cfg.writing_rules_mode == "enforce"
    assert cfg.writing_rules_exclude_paths == ["archive/*", "*.txt"]
