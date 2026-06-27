"""Build commit messages with 7 audit trailers."""
from __future__ import annotations

from typing import Literal


def build_commit_message(
    *,
    subject: str,
    source_session: str,
    agent_identity: str,
    confidence_original: float,
    operator_confirmed: bool,
    proposal_type: Literal["memory", "edit"],
    target_tier: str,
    target_path: str,
) -> str:
    """Return a commit message with subject + blank line + 7 trailers.

    Raises ValueError if any trailer value contains a newline (which would
    break git's trailer parsing).
    """
    for label, value in [
        ("subject", subject),
        ("source_session", source_session),
        ("agent_identity", agent_identity),
        ("target_tier", target_tier),
        ("target_path", target_path),
    ]:
        if "\n" in value or "\r" in value:
            raise ValueError(f"trailer value for {label!r} contains newline")

    trailers = [
        f"KB-Source-Session: {source_session}",
        f"KB-Agent-Identity: {agent_identity}",
        f"KB-Confidence: {confidence_original:.2f}",
        f"KB-Operator-Confirmed: {'yes' if operator_confirmed else 'no'}",
        f"KB-Proposal-Type: {proposal_type}",
        f"KB-Target-Tier: {target_tier}",
        f"KB-Target-Path: {target_path}",
    ]
    return subject + "\n\n" + "\n".join(trailers) + "\n"
