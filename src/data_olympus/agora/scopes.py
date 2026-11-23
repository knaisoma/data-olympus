"""Agora scope ids. They mirror the KB tiers and are the same strings the
index stores in ``docs.category`` (``project:x``, ``stack:y``,
``component:x/z``), plus ``universal``."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from data_olympus.agora.errors import AgoraError

# Always used with fullmatch, so a trailing newline never passes as `$` would allow.
SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

ScopeTier = Literal["T1", "T2", "T3", "T4"]


@dataclass(frozen=True, slots=True)
class ScopeRef:
    id: str
    tier: ScopeTier
    project: str | None = None
    component: str | None = None
    stack: str | None = None


def _segment(value: str, raw: str) -> str:
    # A segment never contains '/', so '..' cannot climb anywhere, but it is
    # refused anyway: a scope id is never a path and should not look like one.
    if not SEGMENT_PATTERN.fullmatch(value) or ".." in value:
        raise AgoraError("invalid", f"invalid scope {raw[:80]!r}")
    return value


def parse_scope(raw: str) -> ScopeRef:
    """Parse ``universal``, ``stack:<s>``, ``project:<p>`` or
    ``component:<p>/<c>``; anything else raises ``AgoraError("invalid")``."""
    if not isinstance(raw, str):
        raise AgoraError("invalid", "scope must be a string")
    if raw == "universal":
        return ScopeRef("universal", "T1")
    kind, sep, rest = raw.partition(":")
    if not sep:
        raise AgoraError("invalid", f"invalid scope {raw[:80]!r}")
    if kind == "stack":
        return ScopeRef(raw, "T2", stack=_segment(rest, raw))
    if kind == "project":
        return ScopeRef(raw, "T3", project=_segment(rest, raw))
    if kind == "component":
        project, slash, component = rest.partition("/")
        if not slash:
            raise AgoraError("invalid", f"invalid scope {raw[:80]!r}")
        return ScopeRef(raw, "T4", project=_segment(project, raw),
                        component=_segment(component, raw))
    raise AgoraError("invalid", f"invalid scope {raw[:80]!r}")


def project_scope_id(project: str) -> str:
    return f"project:{project}"
