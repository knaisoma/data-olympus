"""Shared dependencies of the Agora services."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from data_olympus.agora.models import Participant, ParticipantRecord
from data_olympus.principals import CAP_AGORA_ADMIN

if TYPE_CHECKING:
    from collections.abc import Callable

    from data_olympus.agora.store import AgoraStore
    from data_olympus.audit_log import AuditLog
    from data_olympus.config import Config
    from data_olympus.principals import Principal

CATEGORY_CACHE_SEC = 60.0


@dataclass(frozen=True, slots=True)
class AgoraSettings:
    auth_on: bool
    local_person: str
    agent_votes: bool
    agent_vote_threshold: float
    max_body_bytes: int
    decision_timeout_sec: int

    @classmethod
    def from_config(cls, config: Config, *, auth_on: bool) -> AgoraSettings:
        return cls(
            auth_on=auth_on,
            local_person=config.agora_local_person,
            agent_votes=config.agora_agent_votes,
            agent_vote_threshold=config.agora_agent_vote_threshold,
            max_body_bytes=config.agora_max_body_bytes,
            decision_timeout_sec=config.agora_decision_timeout_sec,
        )


def body_fields(text: str) -> dict[str, Any]:
    """What the audit log keeps of a body: its hash and size, never the text."""
    data = text.encode("utf-8")
    return {"body_sha256": hashlib.sha256(data).hexdigest(), "body_bytes": len(data)}


class AgoraContext:
    def __init__(
        self,
        *,
        store: AgoraStore,
        audit: AuditLog,
        settings: AgoraSettings,
        kb_categories: Callable[[], set[str]],
        principal_lookup: Callable[[str], Principal | None],
        principal_list: Callable[[], list[Principal]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.audit = audit
        self.settings = settings
        self.kb_categories = kb_categories
        self.principal_lookup = principal_lookup
        self.principal_list = principal_list
        self.clock = clock
        self._categories: set[str] = set()
        self._categories_at: float | None = None

    def categories(self) -> set[str]:
        """The KB's scope categories, cached for 60 s."""
        now = self.clock()
        if self._categories_at is None or now - self._categories_at >= CATEGORY_CACHE_SEC:
            try:
                self._categories = set(self.kb_categories())
            except Exception:  # noqa: BLE001 - a broken index must not break Agora
                self._categories = set()
            self._categories_at = now
        return set(self._categories)

    def record(self, action: str, actor: Participant | None, **fields: Any) -> None:
        event: dict[str, Any] = {
            "ts": self.clock(),
            "event": f"agora.{action}",
            "actor": actor.name if actor else "system",
            "actor_kind": actor.kind if actor else "system",
            "via": actor.via if actor else "",
            "verified": actor.verified if actor else True,
        }
        event.update(fields)
        self.audit.append(event)

    def known(self, name: str) -> ParticipantRecord | None:
        """A participant Agora knows: a configured principal with auth on, a
        participant seen before with auth off."""
        if self.settings.auth_on:
            principal = self.principal_lookup(name)
            if principal is None:
                return None
            seen = self.store.get_participant(name)
            return ParticipantRecord(
                principal.name, principal.kind, principal.owner, True,
                seen.first_seen if seen else 0.0, seen.last_seen if seen else 0.0,
            )
        if name == self.settings.local_person:
            seen = self.store.get_participant(name)
            return ParticipantRecord(name, "person", None, False,
                                     seen.first_seen if seen else 0.0,
                                     seen.last_seen if seen else 0.0)
        return self.store.get_participant(name)

    def participant_for(self, name: str) -> Participant | None:
        """The Participant another call by ``name`` would resolve to."""
        rec = self.known(name)
        if rec is None:
            return None
        if self.settings.auth_on:
            principal = self.principal_lookup(name)
            admin = bool(principal and principal.has(CAP_AGORA_ADMIN))
            return Participant(rec.name, rec.kind, rec.owner, True, admin=admin)
        admin = name == self.settings.local_person
        return Participant(rec.name, rec.kind, rec.owner, False, admin=admin)

    def all_names(self) -> list[str]:
        if self.settings.auth_on and self.principal_list is not None:
            return [p.name for p in self.principal_list()]
        names = [r.name for r in self.store.list_participants()]
        if not self.settings.auth_on and self.settings.local_person not in names:
            names.append(self.settings.local_person)
        return names

    def agents_of(self, person: str) -> list[str]:
        out = []
        for name in self.all_names():
            rec = self.known(name)
            if rec is not None and rec.kind == "agent" and rec.owner == person:
                out.append(name)
        return sorted(out)
