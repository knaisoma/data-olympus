"""Resolve the Agora participant behind one call.

With auth on, identity comes only from the authenticated principal: its name,
kind and owner are operator configuration. A self-declared ``agent_identity``
is kept as an unverified ``via`` label and grants nothing. With auth off
(trusted local mode) participants self-declare, and every self-declared agent
is owned by the one local person.
"""
from __future__ import annotations

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.models import Participant
from data_olympus.config import AGORA_NAME_PATTERN
from data_olympus.principals import CAP_AGORA_ADMIN, Principal

VIA_MAX = 64


def valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(AGORA_NAME_PATTERN.fullmatch(name))


def resolve_participant(
    principal: Principal, *, agent_identity: str, auth_on: bool, local_person: str,
) -> Participant:
    identity = (agent_identity or "").strip()
    if auth_on:
        if not principal.authenticated:
            raise AgoraError("forbidden", "authentication required")
        return Participant(
            name=principal.name,
            kind=principal.kind,
            owner=principal.owner,
            verified=True,
            via=identity[:VIA_MAX],
            admin=principal.has(CAP_AGORA_ADMIN),
        )
    if not identity or identity == local_person:
        return Participant(local_person, "person", None, verified=False, admin=True)
    if not valid_name(identity):
        raise AgoraError("invalid", "agent_identity is not a valid participant name")
    return Participant(identity, "agent", local_person, verified=False)
