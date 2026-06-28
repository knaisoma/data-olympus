"""Identity + capability model for write authorization.

A :class:`Principal` is resolved from an ``Authorization: Bearer`` header against
a :class:`PrincipalRegistry` built from ``KB_AUTH_TOKEN`` (back-compat: a single
full-capability principal named ``operator``) plus an optional
``KB_AUTH_PRINCIPALS`` JSON list of per-agent tokens with explicit capabilities.

This unifies three review findings into one mechanism:

- **MCP write-tool auth** — the MCP middleware enforces capabilities for write
  tools exactly as the REST layer does, closing the gap where MCP tools bypassed
  ``KB_AUTH_TOKEN``.
- **Per-agent identity & capability policy** — different tokens map to principals
  with different capability sets.
- **Confidence clamp** — a principal that lacks the ``auto_commit`` capability
  has its proposals parked as *pending* regardless of the client-asserted
  confidence, so a caller cannot self-assert ``confidence: 1.0`` to skip review.

Posture summary:

- **No auth configured at all** (no token, no principals): every caller is the
  fully-trusted ``LOCAL_TRUSTED`` principal. This preserves the pre-auth
  trusted-local behavior and is documented as the trusted-agent assumption.
- **Auth configured**: an unknown/missing token is the read-only ``anonymous``
  principal — denied on every write route. A valid token is its mapped
  principal, with writes gated and possibly confidence-clamped by its capabilities.
"""
from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("data_olympus")

CAP_READ = "read"
CAP_PROPOSE = "propose"
CAP_AUTO_COMMIT = "auto_commit"
CAP_RESOLVE = "resolve"
CAP_BOOTSTRAP = "bootstrap"
CAP_RECORD_EVENT = "record_event"

ALL_CAPABILITIES: frozenset[str] = frozenset({
    CAP_READ, CAP_PROPOSE, CAP_AUTO_COMMIT,
    CAP_RESOLVE, CAP_BOOTSTRAP, CAP_RECORD_EVENT,
})

# Write tools (MCP) / routes (REST) mapped to the capability they require.
WRITE_TOOL_CAPABILITY: dict[str, str] = {
    "kb_propose_memory": CAP_PROPOSE,
    "kb_propose_edit": CAP_PROPOSE,
    "kb_resolve_pending": CAP_RESOLVE,
    "kb_bootstrap_project": CAP_BOOTSTRAP,
    "kb_record_event": CAP_RECORD_EVENT,
}


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    capabilities: frozenset[str]
    authenticated: bool = True

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def can_auto_commit(self) -> bool:
        return CAP_AUTO_COMMIT in self.capabilities


# Used when no auth is configured (trusted-local mode). authenticated=False so
# the "auth configured" branches never treat it as a real bearer principal, but
# it holds every capability so behavior matches the pre-auth product.
LOCAL_TRUSTED = Principal(
    name="local", capabilities=ALL_CAPABILITIES, authenticated=False
)
ANONYMOUS = Principal(
    name="anonymous", capabilities=frozenset({CAP_READ}), authenticated=False
)


def _extract_bearer(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return None
    return auth_header[len(prefix):]


def parse_principals_env(raw: str) -> list[dict[str, Any]]:
    """Parse a ``KB_AUTH_PRINCIPALS`` JSON value. Empty / malformed input degrades
    to an empty list (logged) so a bad config never crashes startup."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("KB_AUTH_PRINCIPALS is not valid JSON; ignoring: %s", exc)
        return []
    if not isinstance(data, list):
        log.warning("KB_AUTH_PRINCIPALS must be a JSON list; ignoring")
        return []
    return [d for d in data if isinstance(d, dict)]


class PrincipalRegistry:
    """Maps bearer tokens to principals; resolves an Authorization header."""

    def __init__(
        self, *, auth_token: str = "", principals: list[dict[str, Any]] | None = None
    ) -> None:
        self._by_token: list[tuple[str, Principal]] = []
        if auth_token:
            self._by_token.append(
                (auth_token, Principal("operator", ALL_CAPABILITIES, authenticated=True))
            )
        for spec in principals or []:
            token = str(spec.get("token", "")).strip()
            if not token:
                continue
            name = str(spec.get("name", "")).strip() or "agent"
            caps = spec.get("capabilities")
            if caps is None:
                capset = ALL_CAPABILITIES
            else:
                capset = frozenset(str(c).strip() for c in caps if str(c).strip())
            self._by_token.append(
                (token, Principal(name, capset, authenticated=True))
            )

    @property
    def auth_configured(self) -> bool:
        return bool(self._by_token)

    def resolve(self, auth_header: str | None) -> Principal:
        if not self.auth_configured:
            return LOCAL_TRUSTED
        token = _extract_bearer(auth_header)
        if token is not None:
            for known, principal in self._by_token:
                if hmac.compare_digest(token.encode(), known.encode()):
                    return principal
        return ANONYMOUS
