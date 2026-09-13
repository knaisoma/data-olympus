"""OKF v0.2 content-change provenance: ``generated: { by, at }``.

Shared by every writer that stamps this metadata (the importers, ``init`` and
server-rendered memories) and by lint, so the actor rule and the structural
checks exist once.

The actor for anything data-olympus renders is always the fixed
``data-olympus/<version>`` tool actor (the OKF actor convention). It is never
derived from a principal name or an agent identity: those are configuration and
client input, and copying a name such as ``human:alice`` into ``generated.by``
would falsely attribute agent-written content to a human author. Who proposed a
memory is recorded separately, in ``created_by``.

The checks here are STRUCTURAL. A string ``generated.at`` is accepted without
validating that its content is an ISO 8601 datetime with an offset; a parsed
``datetime`` (what YAML produces for an unquoted timestamp) must carry an offset.
"""

from __future__ import annotations

import datetime
from typing import Any

TOOL_PRODUCER = "data-olympus"


def tool_actor() -> str:
    """The OKF actor for content data-olympus itself renders."""
    from data_olympus import __version__

    return f"{TOOL_PRODUCER}/{__version__}"


def utc_now_iso(now: datetime.datetime | None = None) -> str:
    """An ISO 8601 UTC datetime to the second, with the ``Z`` offset."""
    moment = (now or datetime.datetime.now(datetime.UTC)).astimezone(datetime.UTC)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tool_generated(at: str | None = None) -> dict[str, str]:
    """A ``generated`` mapping naming the data-olympus tool actor."""
    return {"by": tool_actor(), "at": at if at is not None else utc_now_iso()}


def is_valid_generated_at(value: Any) -> bool:
    """Whether ``generated.at`` is structurally acceptable.

    A non-blank string is accepted as written; its content is not validated. A
    ``datetime`` must be timezone aware. A blank string, a ``date``, a naive
    ``datetime``, a number or null is not acceptable. Blank counts as missing,
    matching how lint has always treated an empty ``timestamp``.
    """
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, datetime.datetime):
        return value.tzinfo is not None and value.utcoffset() is not None
    return False


def generated_problems(value: Any) -> list[str]:
    """Structural problems with a PRESENT ``generated`` value; empty when sound."""
    if not isinstance(value, dict):
        return ["'generated' must be a mapping with 'by' and 'at'"]
    problems: list[str] = []
    by = value.get("by")
    if not isinstance(by, str) or not by.strip():
        problems.append("'generated.by' must be a non-empty actor string")
    if "at" in value and not is_valid_generated_at(value["at"]):
        problems.append(
            "'generated.at' must be an ISO 8601 datetime with an explicit offset"
        )
    return problems


def has_content_change_time(frontmatter: dict[str, Any]) -> bool:
    """Whether a document records when its content last changed.

    Satisfied by a structurally valid ``generated.at`` or by the legacy
    ``timestamp`` that OKF v0.2 supersedes but still permits consumers to read.
    """
    generated = frontmatter.get("generated")
    if (
        isinstance(generated, dict)
        and "at" in generated
        and is_valid_generated_at(generated["at"])
    ):
        return True
    return bool(frontmatter.get("timestamp"))
