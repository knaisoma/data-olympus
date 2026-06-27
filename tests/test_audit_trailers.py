"""Tests for build_commit_message."""
from __future__ import annotations

from data_olympus.audit_trailers import build_commit_message


def test_build_commit_message_includes_subject_and_all_trailers() -> None:
    msg = build_commit_message(
        subject="propose: memory/inbox/2026-06-01-test.md",
        source_session="session-xyz",
        agent_identity="claude",
        confidence_original=0.92,
        operator_confirmed=False,
        proposal_type="memory",
        target_tier="memory",
        target_path="memory/inbox/2026-06-01-test.md",
    )
    assert msg.startswith("propose: memory/inbox/2026-06-01-test.md\n\n")
    assert "KB-Source-Session: session-xyz" in msg
    assert "KB-Agent-Identity: claude" in msg
    assert "KB-Confidence: 0.92" in msg
    assert "KB-Operator-Confirmed: no" in msg
    assert "KB-Proposal-Type: memory" in msg
    assert "KB-Target-Tier: memory" in msg
    assert "KB-Target-Path: memory/inbox/2026-06-01-test.md" in msg


def test_build_commit_message_preserves_original_confidence_after_resolve() -> None:
    """Original confidence is preserved even when a low-confidence
    pending is later approved by the operator (the operator-confirmed flag becomes
    yes, but the confidence number stays the original)."""
    msg = build_commit_message(
        subject="propose: ...",
        source_session="s",
        agent_identity="claude",
        confidence_original=0.42,  # was below threshold
        operator_confirmed=True,    # but operator approved
        proposal_type="edit",
        target_tier="T2",
        target_path="tech-stacks/backend-nestjs/STD-BN-001.md",
    )
    assert "KB-Confidence: 0.42" in msg
    assert "KB-Operator-Confirmed: yes" in msg


def test_build_commit_message_handles_unicode_in_subject_and_session() -> None:
    msg = build_commit_message(
        subject="propose: ユニコード",
        source_session="session-日本語",
        agent_identity="claude",
        confidence_original=0.9,
        operator_confirmed=False,
        proposal_type="memory",
        target_tier="memory",
        target_path="memory/inbox/x.md",
    )
    assert "ユニコード" in msg
    assert "KB-Source-Session: session-日本語" in msg


def test_build_commit_message_rejects_newline_in_session() -> None:
    """Newlines in trailer values would break git's trailer parsing; reject."""
    import pytest
    with pytest.raises(ValueError):
        build_commit_message(
            subject="x",
            source_session="bad\nvalue",
            agent_identity="claude",
            confidence_original=0.9,
            operator_confirmed=False,
            proposal_type="memory",
            target_tier="memory",
            target_path="memory/inbox/x.md",
        )
