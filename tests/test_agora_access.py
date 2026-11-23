"""Agora scopes, membership and visibility rules (issue #492, Task 6)."""
# ruff: noqa: F811 - pytest fixtures imported from tests.agora_world
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.models import Thread
from tests.agora_world import (  # noqa: F401 - pytest fixtures
    access,
    admin,
    ana,
    ana_claude,
    ben,
    ben_claude,
    carol,
    core,
    ctx,
    hs,
    local_agent,
    rogue,
    svc,
    svc_no_auth,
)

if TYPE_CHECKING:
    from data_olympus.agora.models import Participant, Scope
    from data_olympus.agora.service import AgoraService


def _alpha_owned_by(svc: AgoraService, owner: Participant) -> Scope:
    return svc.access.resolve_scope(owner, "project:alpha", claim=True)


def _member(svc: AgoraService, owner: Participant, who: Participant) -> None:
    svc.membership(owner, action="invite", project="project:alpha",
                   participant=who.name)
    svc.membership(who, action="accept", project="project:alpha",
                   participant=who.name)


def test_first_person_claims_kb_project(svc: AgoraService, ana: Participant) -> None:
    scope = svc.access.resolve_scope(ana, "project:alpha", claim=True)
    assert scope.owner == "ana" and scope.has_kb_docs is True
    m = svc.ctx.store.get_membership("project:alpha", "ana")
    assert m is not None and m.role == "owner" and m.state == "active"


def test_second_claim_does_not_steal(svc: AgoraService, ana: Participant,
                                     ben: Participant) -> None:
    _alpha_owned_by(svc, ana)
    assert svc.access.resolve_scope(ben, "project:alpha", claim=True).owner == "ana"
    assert svc.ctx.store.get_membership("project:alpha", "ben") is None


def test_agent_cannot_claim(svc: AgoraService, ana_claude: Participant) -> None:
    assert svc.access.resolve_scope(ana_claude, "project:alpha", claim=True).owner is None


def test_component_ownership_comes_from_project(svc: AgoraService, ana: Participant,
                                                ana_claude: Participant,
                                                ben: Participant) -> None:
    svc.scope_create(ana, "component:alpha/api")
    comp = svc.access.resolve_scope(ana, "component:alpha/api", claim=False)
    assert svc.access.can_read_scope(ana, comp)
    assert svc.access.can_read_scope(ana_claude, comp)
    assert not svc.access.can_read_scope(ben, comp)


def test_agent_inherits_owner_membership(svc: AgoraService, ana: Participant,
                                         ana_claude: Participant,
                                         ben: Participant) -> None:
    alpha = _alpha_owned_by(svc, ana)
    assert svc.access.can_read_scope(ana_claude, alpha)
    assert not svc.access.can_read_scope(ben, alpha)


def test_autonomous_agent_needs_own_invite(svc: AgoraService, ana: Participant,
                                           rogue: Participant) -> None:
    alpha = _alpha_owned_by(svc, ana)
    assert not svc.access.can_read_scope(rogue, alpha)
    svc.membership(ana, action="invite", project="project:alpha", participant="rogue")
    assert not svc.access.can_read_scope(rogue, alpha)  # invited is not active
    svc.membership(rogue, action="accept", project="project:alpha", participant="rogue")
    assert svc.access.can_read_scope(rogue, alpha)


def test_owned_agent_cannot_be_invited_alone(svc: AgoraService, ana: Participant) -> None:
    _alpha_owned_by(svc, ana)
    with pytest.raises(AgoraError) as e:
        svc.membership(ana, action="invite", project="project:alpha",
                       participant="ben-claude")
    assert e.value.code == "invalid"


def test_blocking_owner_cuts_agent_access(svc: AgoraService, ana: Participant,
                                          ben: Participant,
                                          ana_claude: Participant) -> None:
    alpha = _alpha_owned_by(svc, ben)
    _member(svc, ben, ana)
    assert svc.access.can_read_scope(ana_claude, alpha)
    svc.membership(ben, action="block", project="project:alpha", participant="ana")
    assert not svc.access.can_read_scope(ana_claude, alpha)
    assert not svc.access.can_read_scope(ana, alpha)


def test_owner_removal_or_instance_block_cuts_agent(svc: AgoraService,
                                                    admin: Participant,
                                                    ana: Participant,
                                                    ben: Participant,
                                                    ana_claude: Participant) -> None:
    alpha = _alpha_owned_by(svc, ben)
    universal = svc.access.resolve_scope(ana_claude, "universal", claim=False)
    python = svc.access.resolve_scope(ana_claude, "stack:python", claim=False)
    _member(svc, ben, ana)
    svc.membership(ben, action="remove", project="project:alpha", participant="ana")
    assert not svc.access.can_read_scope(ana_claude, alpha)
    _member(svc, ben, ana)
    assert svc.access.can_read_scope(ana_claude, alpha)
    assert svc.access.can_read_scope(ana_claude, universal)
    svc.membership(admin, action="block", project="*", participant="ana")
    for scope in (alpha, universal, python):
        assert not svc.access.can_read_scope(ana_claude, scope)
        assert not svc.access.can_read_scope(ana, scope)
    svc.membership(admin, action="unblock", project="*", participant="ana")
    assert svc.access.can_read_scope(ana_claude, universal)
    assert svc.access.can_read_scope(ana_claude, alpha)


def test_blocked_agent_alone_is_cut(svc: AgoraService, ana: Participant,
                                    ana_claude: Participant) -> None:
    alpha = _alpha_owned_by(svc, ana)
    svc.membership(ana, action="block", project="project:alpha",
                   participant="ana-claude")
    assert not svc.access.can_read_scope(ana_claude, alpha)
    assert svc.access.can_read_scope(ana, alpha)


def test_instance_block_is_admin_only(svc: AgoraService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        svc.membership(ana, action="block", project="*", participant="ben")
    assert e.value.code == "forbidden"


def test_can_read_thread_direct_audience(svc: AgoraService, ana: Participant,
                                         ben: Participant, carol: Participant,
                                         ana_claude: Participant,
                                         admin: Participant) -> None:
    svc.access.resolve_scope(ana, "universal", claim=False)
    th = Thread("th_1", "universal", "participant", "ben", "question", "Q", "ana",
                "open", (), {}, 1.0, 1.0, None)
    assert svc.access.can_read_thread(ana, th)
    assert svc.access.can_read_thread(ben, th)
    # Only the two ends, their owners and admins: the originator's own agent
    # is neither an end nor an owner.
    assert not svc.access.can_read_thread(ana_claude, th)
    assert svc.access.can_read_thread(admin, th)
    assert not svc.access.can_read_thread(carol, th)
    to_agent = Thread("th_2", "universal", "participant", "ben-claude", "question", "Q",
                      "ana", "open", (), {}, 1.0, 1.0, None)
    assert svc.access.can_read_thread(ben, to_agent)  # the addressee's owner


def test_unknown_scope_not_found_and_person_can_create(svc: AgoraService,
                                                       ana: Participant) -> None:
    with pytest.raises(AgoraError, match="not_found"):
        svc.access.resolve_scope(ana, "project:new", claim=False)
    s = svc.scope_create(ana, "project:new")
    assert s.has_kb_docs is False and s.owner == "ana"


def test_auth_off_skips_membership(svc_no_auth: AgoraService,
                                   local_agent: Participant) -> None:
    alpha = svc_no_auth.access.resolve_scope(local_agent, "project:alpha", claim=True)
    assert svc_no_auth.access.can_read_scope(local_agent, alpha)
    assert alpha.owner is None  # claims only happen with auth on
