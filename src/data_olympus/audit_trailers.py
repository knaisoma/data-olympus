"""Build commit messages with 7 audit trailers, plus an optional claim link."""
from __future__ import annotations

from typing import Literal

# Everything ``str.splitlines`` treats as a line boundary beyond CR and LF.
_LINE_SEPARATORS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
                    "\u2028", "\u2029")


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
    pending_id: str | None = None,
) -> str:
    """Return a commit message with subject + blank line + the audit trailers.

    ``pending_id`` adds an eighth trailer, ``KB-Pending-Id``, naming the pending
    claim this commit satisfied. It is what lets recovery decide whether an
    interrupted resolve actually committed: the commit message and the tree
    belong to the same commit object, so there is no interval in which the
    content is committed and the link is not. Omitted for an auto-committed
    write, which satisfies no operator decision.

    Raises ValueError if any trailer value contains a newline (which would
    break git's trailer parsing).
    """
    for label, value in [
        ("subject", subject),
        ("source_session", source_session),
        ("agent_identity", agent_identity),
        ("target_tier", target_tier),
        ("target_path", target_path),
        ("pending_id", pending_id or ""),
    ]:
        if "\n" in value or "\r" in value:
            raise ValueError(f"trailer value for {label!r} contains newline")
        # Rejecting only CR and LF is not enough. Python's ``str.splitlines``
        # (and several other consumers) also break on U+2028, U+2029, U+0085
        # and the vertical tab / form feed / file separator family, while git
        # does not. A value carrying one reads as a single trailer to git and
        # as SEVERAL trailers downstream, which is enough to manufacture a
        # trailer block git never wrote. The parser in ``git_ops`` splits on LF
        # for exactly this reason; refusing them here as well means such a
        # value cannot be committed in the first place.
        if any(ch in value for ch in _LINE_SEPARATORS):
            raise ValueError(
                f"trailer value for {label!r} contains a unicode line separator"
            )

    trailers = [
        f"KB-Source-Session: {source_session}",
        f"KB-Agent-Identity: {agent_identity}",
        f"KB-Confidence: {confidence_original:.2f}",
        f"KB-Operator-Confirmed: {'yes' if operator_confirmed else 'no'}",
        f"KB-Proposal-Type: {proposal_type}",
        f"KB-Target-Tier: {target_tier}",
        f"KB-Target-Path: {target_path}",
    ]
    if pending_id:
        trailers.append(f"KB-Pending-Id: {pending_id}")
    return subject + "\n\n" + "\n".join(trailers) + "\n"
