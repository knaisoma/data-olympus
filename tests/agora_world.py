"""Shared fixtures for the Agora service tests (issue #492).

The world: a temporary SQLite store and audit log, KB categories
``project:alpha`` and ``stack:python``, and a registry with ana and ben
(persons), ana-claude (an agent owned by ana), ben-claude (owned by ben), rogue
(an autonomous agent), admin (a person with agora_admin) and carol (a person).
Auth is on unless a test builds the no-auth world.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from data_olympus.agora.access import Access
from data_olympus.agora.context import AgoraContext, AgoraSettings
from data_olympus.agora.identity import resolve_participant
from data_olympus.agora.service import AgoraService
from data_olympus.agora.sqlite_store import SQLiteAgoraStore
from data_olympus.audit_log import AuditLog
from data_olympus.principals import LOCAL_TRUSTED, PrincipalRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from data_olympus.agora.models import Participant

PRINCIPALS: list[dict[str, Any]] = [
    {"name": "ana", "token": "t-ana", "kind": "person"},
    {"name": "ben", "token": "t-ben", "kind": "person"},
    {"name": "carol", "token": "t-carol", "kind": "person"},
    {"name": "ana-claude", "token": "t-ac", "owner": "ana"},
    {"name": "ben-claude", "token": "t-bc", "owner": "ben"},
    {"name": "rogue", "token": "t-rogue"},
    {"name": "admin", "token": "t-admin", "kind": "person",
     "capabilities": ["read", "agora_admin"]},
]


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


def make_ctx(tmp_path: Path, *, auth_on: bool = True,
             categories: set[str] | None = None) -> AgoraContext:
    registry = PrincipalRegistry(principals=PRINCIPALS, agora_implicit_caps=True)
    store = SQLiteAgoraStore(str(tmp_path / "agora" / "agora.db"))
    store.initialize()
    audit = AuditLog(log_path=str(tmp_path / "agora" / "audit.log"))
    settings = AgoraSettings(
        auth_on=auth_on, local_person="aj", agent_votes=True,
        agent_vote_threshold=0.85, max_body_bytes=32768,
        decision_timeout_sec=604800,
    )
    cats = categories if categories is not None else {"project:alpha", "stack:python"}
    return AgoraContext(
        store=store, audit=audit, settings=settings,
        kb_categories=lambda: set(cats),
        principal_lookup=registry.get if auth_on else (lambda _n: None),
        principal_list=registry.principals if auth_on else None,
        clock=Clock(),
    )


def participant(name: str) -> Participant:
    registry = PrincipalRegistry(principals=PRINCIPALS, agora_implicit_caps=True)
    principal = registry.get(name)
    assert principal is not None
    return resolve_participant(principal, agent_identity="", auth_on=True,
                               local_person="aj")


def local(identity: str = "") -> Participant:
    return resolve_participant(LOCAL_TRUSTED, agent_identity=identity, auth_on=False,
                               local_person="aj")


@pytest.fixture
def ctx(tmp_path: Path) -> AgoraContext:
    return make_ctx(tmp_path)


@pytest.fixture
def access(ctx: AgoraContext) -> Access:
    return Access(ctx)


@pytest.fixture
def svc(ctx: AgoraContext, access: Access) -> AgoraService:
    return AgoraService(ctx, access)


@pytest.fixture
def core(svc: AgoraService) -> AgoraService:
    return svc


@pytest.fixture
def svc_no_auth(tmp_path: Path) -> AgoraService:
    c = make_ctx(tmp_path / "noauth", auth_on=False)
    return AgoraService(c, Access(c))


@pytest.fixture
def ana() -> Participant:
    return participant("ana")


@pytest.fixture
def ben() -> Participant:
    return participant("ben")


@pytest.fixture
def carol() -> Participant:
    return participant("carol")


@pytest.fixture
def ana_claude() -> Participant:
    return participant("ana-claude")


@pytest.fixture
def ben_claude() -> Participant:
    return participant("ben-claude")


@pytest.fixture
def rogue() -> Participant:
    return participant("rogue")


@pytest.fixture
def admin() -> Participant:
    return participant("admin")


@pytest.fixture
def local_agent() -> Participant:
    return local("claude")
