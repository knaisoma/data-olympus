"""Tests for the secret-scanning gate on the write path (issue #71).

Covers:
  - the low-level scanner (``write_gate.scan_postimage_for_secrets``): one
    built-in pattern class per test, redaction, custom extra patterns, invalid
    extra-pattern tolerance, and clean-content regression;
  - the auto-commit path (``kb_propose_memory_fn`` / ``kb_propose_edit_fn``):
    a flagged postimage is rejected ``rejected_secret_detected`` and nothing is
    committed or queued as pending;
  - the pending resolve-approve path (``kb_resolve_pending_fn``): a flagged
    ``edited_text`` is rejected and the entry stays pending; an explicit
    operator override commits it anyway and the audit event records the
    override;
  - the onboarding bootstrap path (``kb_bootstrap_project_fn``): a flagged
    bootstrap file postimage is rejected before any commit;
  - that no rejection response, pending-queue entry, or audit event ever
    contains the literal secret value.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from data_olympus.audit_log import AuditLog
from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_onboarding import kb_bootstrap_project_fn
from data_olympus.tools_write import (
    kb_propose_edit_fn,
    kb_propose_memory_fn,
    kb_resolve_pending_fn,
)
from data_olympus.worktrees import WorktreeRegistry
from data_olympus.write_gate import (
    load_extra_secret_patterns,
    scan_postimage_for_secrets,
)

# ---- sample secrets, one per built-in pattern class ------------------------
#
# Every fixture below is built by concatenating string fragments rather than
# one contiguous literal. The VALUES are still synthetic, never-real
# credentials -- but a contiguous literal in this exact shape is enough for
# GitHub's own push-protection secret scanner to flag the commit (it happened
# during development of this very test file). Splitting the literal keeps the
# fragments out of a single scannable token in the tracked source while the
# runtime-concatenated string is still exactly what scan_postimage_for_secrets
# needs to see to exercise each pattern.

PRIVATE_KEY = (
    "-----BEGIN " + "RSA PRIVATE KEY" + "-----\n"
    "MIIEpAIBAAKCAQEA" + "1234567890abcdef" + "\n"
    "-----END " + "RSA PRIVATE KEY" + "-----"
)
GITHUB_TOKEN = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"
GITHUB_PAT = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz01234567890"
AWS_ACCESS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
SLACK_TOKEN = "xoxb-" + "1234567890-abcdefghijklmnopqrst"
GENERIC_CRED = "password" + "=" + "Sup3rSecretValue!"
CONN_STRING = "postgres://dbuser:" + "Sup3rSecretValue!" + "@db.example.com:5432/kb"

ALL_BUILTIN_SECRETS = {
    "private_key_block": PRIVATE_KEY,
    "github_token": GITHUB_TOKEN,
    "aws_access_key_id": AWS_ACCESS_KEY,
    "slack_token": SLACK_TOKEN,
    "generic_credential_assignment": GENERIC_CRED,
    "connection_string_password": CONN_STRING,
}


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _set_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


def _state(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "seed.md"], cwd=repo, check=True, env=_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_env())
    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    rl = SlidingWindowLimiter(max_per_hour=1000)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    return git, reg, pq, pen, rl, bl


# ============================================================================
# Unit tests: scan_postimage_for_secrets
# ============================================================================


@pytest.mark.parametrize("pattern_name,secret", sorted(ALL_BUILTIN_SECRETS.items()))
def test_scan_detects_each_builtin_pattern_class(pattern_name, secret) -> None:
    content = f"# doc\n\nsome text\n{secret}\nmore text\n"
    result = scan_postimage_for_secrets(postimage=content)
    assert not result.ok
    assert result.match is not None
    assert result.match.pattern_name == pattern_name


def test_scan_clean_content_passes() -> None:
    content = "---\ncreated_by: claude\n---\n\nThis is a perfectly normal memory.\n"
    result = scan_postimage_for_secrets(postimage=content)
    assert result.ok
    assert result.match is None


def test_scan_detects_github_fine_grained_pat() -> None:
    """The longer-lived github_pat_ token format is grouped under the same
    'github_token' pattern name as ghp_/gho_/ghs_/ghr_."""
    result = scan_postimage_for_secrets(postimage=f"token: {GITHUB_PAT}\n")
    assert not result.ok
    assert result.match.pattern_name == "github_token"


def test_scan_generic_credential_placeholder_is_not_flagged() -> None:
    """False-positive handling: an obvious placeholder value must not trip the
    generic credential-assignment pattern."""
    for placeholder in ("password=changeme", "password=<your password>",
                        "password=", 'password=""', "secret=REDACTED"):
        result = scan_postimage_for_secrets(postimage=placeholder)
        assert result.ok, f"{placeholder!r} should not be flagged"


def test_scan_generic_credential_catches_prefixed_env_style_keys() -> None:
    """Real leaks are far more often an env-style prefixed key
    (``DB_PASSWORD=``, ``API_SECRET=``) than a bare ``password=``; a plain
    ``\\bpassword`` regex would miss these since ``_`` is a word character with
    no boundary before ``PASSWORD`` in ``DB_PASSWORD``."""
    for content in ("DB_PASSWORD=Sup3rSecretValue!", "API_SECRET: Sup3rSecretValue!",
                    "MYSQL_ROOT_PASSWORD=Sup3rSecretValue!"):
        result = scan_postimage_for_secrets(postimage=content)
        assert not result.ok, f"{content!r} should be flagged"
        assert result.match.pattern_name == "generic_credential_assignment"


def test_scan_reports_approximate_line_number() -> None:
    content = "line1\nline2\nline3\n" + GITHUB_TOKEN + "\nline5\n"
    result = scan_postimage_for_secrets(postimage=content)
    assert not result.ok
    assert result.match.line == 4


def test_scan_result_never_contains_the_secret_value() -> None:
    content = f"body\n{AWS_ACCESS_KEY}\nmore\n"
    result = scan_postimage_for_secrets(postimage=content)
    assert not result.ok
    # The dataclass has exactly pattern_name + line; the raw secret string must
    # not appear anywhere in its repr.
    assert AWS_ACCESS_KEY not in repr(result)
    assert AWS_ACCESS_KEY not in repr(result.match)


def test_scan_extra_pattern_from_env_rejects() -> None:
    extra = load_extra_secret_patterns("INTERNAL-[0-9]{6}")
    assert len(extra) == 1
    result = scan_postimage_for_secrets(
        postimage="ticket ref INTERNAL-482910 in the body", extra_patterns=extra,
    )
    assert not result.ok
    assert result.match.pattern_name == "custom_1"


def test_scan_invalid_extra_pattern_is_skipped_not_raised() -> None:
    """An invalid regex in KB_SECRET_SCAN_EXTRA_PATTERNS must be logged and
    skipped, never crash the scanner."""
    extra = load_extra_secret_patterns("[unterminated(,INTERNAL-[0-9]{6}")
    # Only the valid entry survives.
    assert len(extra) == 1
    result = scan_postimage_for_secrets(
        postimage="INTERNAL-482910", extra_patterns=extra,
    )
    assert not result.ok


def test_scan_reads_extra_patterns_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("KB_SECRET_SCAN_EXTRA_PATTERNS", "MYCO-SECRET-[A-Z0-9]{8}")
    result = scan_postimage_for_secrets(postimage="key is MYCO-SECRET-ABCD1234 here")
    assert not result.ok
    assert result.match.pattern_name == "custom_1"


def test_scan_no_extra_patterns_env_defaults_clean() -> None:
    result = scan_postimage_for_secrets(postimage="nothing interesting here")
    assert result.ok


# ============================================================================
# Scenario 1 + 2: auto-commit paths (propose_memory, propose_edit, bootstrap)
# ============================================================================


@pytest.mark.parametrize("pattern_name,secret", sorted(ALL_BUILTIN_SECRETS.items()))
def test_propose_memory_rejects_each_secret_pattern(
    tmp_path, monkeypatch, pattern_name, secret,
) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text=f"note containing {secret}", tags=[], source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_secret_detected"
    assert resp.matching_pattern == pattern_name
    assert secret not in (resp.reason or "")
    assert pq.size() == 0
    assert pen.size() == 0
    wt = reg.get_or_create(source_session="s", agent_identity="claude")
    status = subprocess.check_output(
        ["git", "-C", wt.path, "status", "--porcelain"], text=True)
    assert status.strip() == ""


def test_propose_edit_rejects_secret_in_postimage(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path="decisions/DEC-leak.md",
        postimage=f"---\nid: DEC-leak\ntype: decision\nstatus: accepted\n"
                  f"tier: meta\n---\ncreds: {AWS_ACCESS_KEY}\n",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="oops", source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_secret_detected"
    assert resp.matching_pattern == "aws_access_key_id"
    assert AWS_ACCESS_KEY not in (resp.reason or "")
    assert pq.size() == 0
    assert pen.size() == 0


def _bootstrap_pieces(tmp_path, monkeypatch):
    _set_git_env(monkeypatch)
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True)
    (repo / "seed.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    rl = SlidingWindowLimiter(max_per_hour=1000)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    return reg, pq, pen, rl, bl


def test_bootstrap_rejects_file_with_secret(tmp_path, monkeypatch) -> None:
    reg, pq, pen, rl, bl = _bootstrap_pieces(tmp_path, monkeypatch)
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    idx.id_to_path_map.return_value = {}
    files = [
        {"target_path": "projects/p/README.md",
         "postimage": "---\nid: projects-p-README\ntype: project\nstatus: active\n"
                      "tier: T3\n---\n# P\n"},
        {"target_path": "projects/p/AGENTS.md",
         "postimage": f"---\nid: projects-p-AGENTS\ntype: project\nstatus: active\n"
                      f"tier: T3\n---\nkey: {GITHUB_TOKEN}\n"},
    ]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
    )
    assert resp.status == "rejected_secret_detected"
    assert pq.size() == 0
    assert GITHUB_TOKEN not in " ".join(resp.rejected_paths)
    # Nothing staged in the worktree.
    wt = reg.get_or_create(source_session="s", agent_identity="claude")
    status = subprocess.check_output(
        ["git", "-C", wt.path, "status", "--porcelain"], text=True)
    assert status.strip() == ""


# ============================================================================
# Scenario 3 + 7: resolve-approve path (edited_text + operator override)
# ============================================================================


def test_resolve_approve_rejects_edited_text_with_secret(tmp_path, monkeypatch) -> None:
    """A clean pending memory whose edited_text (operator edit) INTRODUCES a
    secret is rejected at resolve time; the entry stays pending, not
    consumed."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="clean body", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert m.status == "pending_confirmation"
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve",
        edited_text=f"edited body with {SLACK_TOKEN}\n",
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
    )
    assert resp.status == "rejected_secret_detected"
    assert SLACK_TOKEN not in (resp.reason or "")
    assert pen.size() == 1, "the pending entry must remain (not consumed)"
    assert pq.size() == 0


def test_resolve_approve_operator_override_commits_flagged_content(
    tmp_path, monkeypatch,
) -> None:
    """An explicit operator override on resolve commits content the scanner
    flagged, and the audit event records that the override was used."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    audit = AuditLog(log_path=str(tmp_path / "audit.log"), hmac_key="")
    m = kb_propose_memory_fn(
        text="clean body", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        audit_log=audit,
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve",
        edited_text=f"edited body with {GENERIC_CRED}\n",
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
        audit_log=audit,
        override_secret_scan=True,
    )
    assert resp.status == "committed"
    assert resp.commit_sha
    assert pen.size() == 0

    events = list(audit.iter_filtered())
    committed = [e for e in events if e.get("status") == "committed"
                 and e.get("event_type") == "resolve"]
    assert len(committed) == 1
    assert committed[0].get("secret_scan_override") is True
    # The audit event must never carry the raw secret value.
    assert GENERIC_CRED.split("=")[1] not in str(committed[0])


def test_resolve_without_override_still_enforced_on_flagged_original(
    tmp_path, monkeypatch,
) -> None:
    """A pending entry whose ORIGINAL (unedited) postimage carries a secret is
    also rejected on a plain approve (no edited_text, no override)."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text=f"body with {PRIVATE_KEY}", tags=[], source_session="s",
        agent_identity="claude", confidence=0.3, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert m.status == "pending_confirmation"
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
    )
    assert resp.status == "rejected_secret_detected"
    assert pen.size() == 1


def test_propose_memory_has_no_override_parameter() -> None:
    """Scenario 7's API-shape guarantee: the auto-commit path has no override
    knob at all, so an agent cannot self-authorize past the gate."""
    import inspect
    sig = inspect.signature(kb_propose_memory_fn)
    assert "override_secret_scan" not in sig.parameters
    sig2 = inspect.signature(kb_propose_edit_fn)
    assert "override_secret_scan" not in sig2.parameters


# ============================================================================
# Scenario 4: clean content regression, every path
# ============================================================================


def test_clean_propose_memory_commits_unchanged(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="a perfectly ordinary memory", tags=["x"], source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"


def test_clean_propose_edit_commits_unchanged(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path="decisions/DEC-clean.md",
        postimage="---\nid: DEC-clean\ntype: decision\nstatus: accepted\n"
                  "tier: meta\n---\nnothing sensitive here\n",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="clean", source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"


def test_clean_resolve_approve_commits_unchanged(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="clean", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve", edited_text="edited clean\n",
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
    )
    assert resp.status == "committed"


def test_clean_bootstrap_commits_unchanged(tmp_path, monkeypatch) -> None:
    reg, pq, pen, rl, bl = _bootstrap_pieces(tmp_path, monkeypatch)
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    idx.id_to_path_map.return_value = {}
    files = [
        {"target_path": "projects/p/README.md",
         "postimage": "---\nid: projects-p-README\ntype: project\nstatus: active\n"
                      "tier: T3\n---\n# P\n"},
        {"target_path": "projects/p/AGENTS.md",
         "postimage": "---\nid: projects-p-AGENTS\ntype: project\nstatus: active\n"
                      "tier: T3\n---\n# rules\n"},
    ]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
    )
    assert resp.status == "committed"


# ============================================================================
# Scenario 6: redaction completeness on the rejection response + audit event
# ============================================================================


def test_rejection_audit_event_carries_pattern_and_line_not_value(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    audit = AuditLog(log_path=str(tmp_path / "audit.log"), hmac_key="")
    resp = kb_propose_memory_fn(
        text=f"line1\nline2\n{AWS_ACCESS_KEY}\n", tags=[], source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4", audit_log=audit,
    )
    assert resp.status == "rejected_secret_detected"
    assert AWS_ACCESS_KEY not in (resp.reason or "")
    assert AWS_ACCESS_KEY not in (resp.matching_pattern or "")

    events = list(audit.iter_filtered())
    rejected = [e for e in events if e.get("status") == "rejected_secret_detected"]
    assert len(rejected) == 1
    ev = rejected[0]
    assert ev.get("matching_pattern") == "aws_access_key_id"
    assert AWS_ACCESS_KEY not in str(ev)
    # Some approximate line locator is present in the reason text.
    assert ev.get("reason")
    assert "line" in ev["reason"].lower()


def test_pending_meta_never_contains_secret_value(tmp_path, monkeypatch) -> None:
    """A LOW-confidence propose is not scanned at enqueue time (the operator
    must be able to see/edit/reject it), but IF the auto-commit path rejects
    a HIGH-confidence proposal, no pending entry (and thus no pending meta) is
    ever created carrying the secret."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text=f"body {GITHUB_TOKEN}", tags=[], source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_secret_detected"
    assert pen.list() == []
