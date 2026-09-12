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


def test_commit_message_links_the_pending_claim() -> None:
    """A commit that satisfied an operator decision names the claim it
    satisfied (issues #253, #254). Recovery searches for this trailer to decide
    whether an interrupted resolve actually committed; without it the commit and
    the decision cannot be connected after a crash."""
    from data_olympus.audit_trailers import build_commit_message

    msg = build_commit_message(
        subject="resolve: operator/notes.md",
        source_session="s1",
        agent_identity="claude",
        confidence_original=0.4,
        operator_confirmed=True,
        proposal_type="edit",
        target_tier="T1",
        target_path="operator/notes.md",
        pending_id="a" * 32,
    )

    assert "KB-Pending-Id: " + "a" * 32 in msg


def test_commit_message_omits_the_pending_trailer_when_there_is_no_claim() -> None:
    """An auto-committed write satisfies no pending decision, so it carries no
    claim link rather than an empty one."""
    from data_olympus.audit_trailers import build_commit_message

    msg = build_commit_message(
        subject="memory: note",
        source_session="s1",
        agent_identity="claude",
        confidence_original=0.9,
        operator_confirmed=False,
        proposal_type="memory",
        target_tier="T1",
        target_path="memory/inbox/n.md",
    )

    assert "KB-Pending-Id" not in msg


def test_commit_message_rejects_unicode_line_separators() -> None:
    """git's trailer parsing breaks on LF only, but other consumers (including
    Python's own splitlines) break on U+2028, U+2029 and U+0085. A value
    carrying one can read as several trailer lines somewhere downstream, so the
    builder refuses them the same way it refuses CR and LF."""
    import pytest

    from data_olympus.audit_trailers import build_commit_message

    for codepoint in (0x2028, 0x2029, 0x0085, 0x000B, 0x000C, 0x001C):
        with pytest.raises(ValueError, match="line separator|newline"):
            build_commit_message(
                subject="memory: note",
                source_session="s1",
                agent_identity="claude",
                confidence_original=0.9,
                operator_confirmed=False,
                proposal_type="memory",
                target_tier="T1",
                target_path=f"decisions/innocent{chr(codepoint)}evil.md",
            )
