"""Agora core service: membership, contacts, profiles, scopes, subscriptions
(issue #492, Task 6)."""
# ruff: noqa: F811 - pytest fixtures imported from tests.agora_world
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_olympus.agora.errors import AgoraError
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
    rogue,
    svc,
    svc_no_auth,
)

if TYPE_CHECKING:
    from data_olympus.agora.models import Participant
    from data_olympus.agora.service import AgoraService


def _claim(svc: AgoraService, who: Participant) -> None:
    svc.access.resolve_scope(who, "project:alpha", claim=True)


def test_last_owner_cannot_be_removed(svc: AgoraService, ana: Participant) -> None:
    _claim(svc, ana)
    for action in ("remove", "block"):
        with pytest.raises(AgoraError) as e:
            svc.membership(ana, action=action, project="project:alpha",  # type: ignore[arg-type]
                           participant="ana")
        assert e.value.code == "conflict"
    with pytest.raises(AgoraError) as e2:
        svc.membership(ana, action="set_role", project="project:alpha",
                       participant="ana", role="member")
    assert e2.value.code == "conflict"


def test_second_owner_allows_removal(svc: AgoraService, ana: Participant,
                                     ben: Participant) -> None:
    _claim(svc, ana)
    svc.membership(ana, action="invite", project="project:alpha", participant="ben")
    svc.membership(ben, action="accept", project="project:alpha", participant="ben")
    svc.membership(ana, action="set_role", project="project:alpha", participant="ben",
                   role="owner")
    m = svc.membership(ana, action="remove", project="project:alpha", participant="ana")
    assert m.state == "removed"
    scope = svc.ctx.store.get_scope("project:alpha")
    assert scope is not None and scope.owner == "ben"


def test_only_persons_can_be_owners(svc: AgoraService, ana: Participant,
                                    rogue: Participant) -> None:
    _claim(svc, ana)
    svc.membership(ana, action="invite", project="project:alpha", participant="rogue")
    svc.membership(rogue, action="accept", project="project:alpha", participant="rogue")
    with pytest.raises(AgoraError) as e:
        svc.membership(ana, action="set_role", project="project:alpha",
                       participant="rogue", role="owner")
    assert e.value.code == "invalid"


def test_only_invitee_or_owner_accepts(svc: AgoraService, ana: Participant,
                                       ben: Participant, carol: Participant) -> None:
    _claim(svc, ben)
    svc.membership(ben, action="invite", project="project:alpha", participant="ana")
    with pytest.raises(AgoraError) as e:
        svc.membership(carol, action="accept", project="project:alpha",
                       participant="ana")
    assert e.value.code == "forbidden"
    assert svc.membership(ana, action="accept", project="project:alpha",
                          participant="ana").state == "active"
    with pytest.raises(AgoraError) as e2:
        svc.membership(ana, action="accept", project="project:alpha",
                       participant="ana")
    assert e2.value.code == "conflict"


def test_member_cannot_invite_but_can_leave(svc: AgoraService, ana: Participant,
                                            ben: Participant) -> None:
    _claim(svc, ben)
    svc.membership(ben, action="invite", project="project:alpha", participant="ana")
    svc.membership(ana, action="accept", project="project:alpha", participant="ana")
    with pytest.raises(AgoraError) as e:
        svc.membership(ana, action="invite", project="project:alpha",
                       participant="carol")
    assert e.value.code == "forbidden"
    assert svc.membership(ana, action="remove", project="project:alpha",
                          participant="ana").state == "removed"


def test_inviting_blocked_is_conflict(svc: AgoraService, ana: Participant) -> None:
    _claim(svc, ana)
    svc.membership(ana, action="block", project="project:alpha", participant="ben")
    with pytest.raises(AgoraError) as e:
        svc.membership(ana, action="invite", project="project:alpha", participant="ben")
    assert e.value.code == "conflict"
    svc.membership(ana, action="unblock", project="project:alpha", participant="ben")
    assert svc.membership(ana, action="invite", project="project:alpha",
                          participant="ben").state == "invited"


def test_blocked_person_cannot_accept_or_claim(svc: AgoraService,
                                               ben: Participant,
                                               admin: Participant) -> None:
    svc.membership(admin, action="block", project="*", participant="ben")
    assert svc.access.resolve_scope(ben, "project:alpha", claim=True).owner is None


def test_membership_on_unknown_participant(svc: AgoraService, ana: Participant) -> None:
    _claim(svc, ana)
    with pytest.raises(AgoraError) as e:
        svc.membership(ana, action="invite", project="project:alpha",
                       participant="nobody")
    assert e.value.code == "not_found"


def test_membership_requires_project_scope(svc: AgoraService, admin: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        svc.membership(admin, action="invite", project="stack:python",
                       participant="ana")
    assert e.value.code == "invalid"


def test_scope_create_rules(svc: AgoraService, ana: Participant, ben: Participant,
                            ana_claude: Participant, admin: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        svc.scope_create(ana_claude, "project:bots")
    assert e.value.code == "forbidden"
    with pytest.raises(AgoraError) as e2:
        svc.scope_create(ana, "stack:rust")
    assert e2.value.code == "forbidden"
    assert svc.scope_create(admin, "stack:rust").tier == "T2"
    with pytest.raises(AgoraError) as e3:
        svc.scope_create(ana, "project:alpha")  # exists in the KB
    assert e3.value.code == "conflict"
    with pytest.raises(AgoraError) as e4:
        svc.scope_create(ana, "universal")
    assert e4.value.code == "invalid"
    comp = svc.scope_create(ana, "component:newproj/api", remote_url="git@x:y.git")
    assert comp.tier == "T4" and comp.remote_url == "git@x:y.git"
    proj = svc.ctx.store.get_scope("project:newproj")
    assert proj is not None and proj.owner == "ana"
    with pytest.raises(AgoraError) as e5:
        svc.scope_create(ben, "component:newproj/web")
    assert e5.value.code == "forbidden"


def test_scopes_lists_readable(svc: AgoraService, ana: Participant,
                               ben: Participant) -> None:
    svc.scope_create(ana, "project:secret")
    ana_ids = {s.id for s in svc.scopes(ana)}
    ben_ids = {s.id for s in svc.scopes(ben)}
    assert {"universal", "stack:python", "project:secret"} <= ana_ids
    assert "project:secret" not in ben_ids and "stack:python" in ben_ids


def test_profile_set_by_owner_and_not_by_stranger(svc: AgoraService, ana: Participant,
                                                  ben: Participant) -> None:
    p = svc.profile_set(ana, target="ana-claude", description="Backend",
                        skills=["Python", "testing", "python"], tool="claude-code")
    assert p.skills == ("python", "testing") and p.description == "Backend"
    with pytest.raises(AgoraError) as e:
        svc.profile_set(ben, target="ana-claude", description="hijack")
    assert e.value.code == "forbidden"
    card = svc.profile_get(ben, "ana-claude")
    assert card["x-agora"]["owner"] == "ana" and card["description"] == "Backend"


def test_profile_validation(svc: AgoraService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        svc.profile_set(ana, skills=["Bad Tag"])
    assert e.value.code == "invalid"
    with pytest.raises(AgoraError) as e2:
        svc.profile_set(ana, skills=[f"s{i}" for i in range(21)])
    assert e2.value.code == "invalid"
    with pytest.raises(AgoraError) as e3:
        svc.profile_set(ana, description="x" * 2001)
    assert e3.value.code == "too_large"
    with pytest.raises(AgoraError) as e4:
        svc.profile_set(ana, description="key ghp_" + "a" * 36)
    assert e4.value.code == "secret_detected"


def test_profile_autonomy_hides_unreadable_scopes(svc: AgoraService, ana: Participant,
                                                  ben: Participant) -> None:
    svc.scope_create(ana, "project:secret")
    svc.subscribe(ana, scope="project:secret", autonomy="reply")
    svc.subscribe(ana, scope="universal")
    assert svc.profile_get(ana, "ana")["x-agora"]["autonomy"] == {
        "project:secret": "reply", "universal": "notify"}
    assert svc.profile_get(ben, "ana")["x-agora"]["autonomy"] == {"universal": "notify"}


def test_profile_get_unknown(svc: AgoraService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        svc.profile_get(ana, "nobody")
    assert e.value.code == "not_found"


def test_directory_lists_readers(svc: AgoraService, ana: Participant,
                                 ben: Participant) -> None:
    svc.scope_create(ana, "project:secret")
    names = {d["name"] for d in svc.directory(ana, "project:secret")}
    assert names == {"ana", "ana-claude", "admin"}
    with pytest.raises(AgoraError) as e:
        svc.directory(ben, "project:secret")
    assert e.value.code == "forbidden"
    assert "ben" in {d["name"] for d in svc.directory(ben, "universal")}


def test_contact_same_accountable_person_is_implicit(svc: AgoraService,
                                                     ana: Participant,
                                                     ana_claude: Participant) -> None:
    assert svc.contact_ok(ana_claude, "ana")
    assert svc.contact_ok(ana, "ana-claude")


def test_contact_request_accept_revoke(svc: AgoraService, ana: Participant,
                                       ben: Participant, ben_claude: Participant,
                                       carol: Participant) -> None:
    assert not svc.contact_ok(ana, "ben-claude")
    c = svc.contact(ana, action="request", other="ben-claude", note="pairing on CI")
    assert c.state == "requested" and not svc.contact_ok(ana, "ben-claude")
    with pytest.raises(AgoraError) as e:
        svc.contact(carol, action="accept", other="ana", recipient="ben-claude")
    assert e.value.code == "forbidden"
    svc.contact(ben, action="accept", other="ana", recipient="ben-claude")
    assert svc.contact_ok(ana, "ben-claude")
    svc.contact(ben_claude, action="revoke", other="ana")
    assert not svc.contact_ok(ana, "ben-claude")


def test_contact_errors(svc: AgoraService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        svc.contact(ana, action="request", other="nobody")
    assert e.value.code == "not_found"
    with pytest.raises(AgoraError) as e2:
        svc.contact(ana, action="revoke", other="ben")
    assert e2.value.code == "not_found"
    with pytest.raises(AgoraError) as e3:
        svc.contact(ana, action="request", other="ana")
    assert e3.value.code == "invalid"


def test_subscribe_requires_readable_scope(svc: AgoraService, ana: Participant,
                                           ben: Participant) -> None:
    svc.scope_create(ana, "project:secret")
    with pytest.raises(AgoraError) as e:
        svc.subscribe(ben, scope="project:secret")
    assert e.value.code == "forbidden"
    with pytest.raises(AgoraError) as e2:
        svc.subscribe(ana, scope="universal", autonomy="yolo")  # type: ignore[arg-type]
    assert e2.value.code == "invalid"
    s = svc.subscribe(ana, scope="universal", autonomy="act", kinds=["question"])
    assert s.autonomy == "act" and s.kinds == ("question",)
    svc.unsubscribe(ana, scope="universal")
    assert svc.ctx.store.list_subscriptions("ana") == []


def test_implicit_scopes(svc: AgoraService, ana: Participant) -> None:
    svc.scope_create(ana, "component:alpha/api")
    assert svc.implicit_scopes("ana") == {"universal", "project:alpha",
                                          "component:alpha/api"}
    assert svc.implicit_scopes("ana-claude") == {"universal", "project:alpha",
                                                 "component:alpha/api"}
    assert svc.implicit_scopes("ben") == {"universal"}


def test_auth_off_skips_membership(svc_no_auth: AgoraService) -> None:
    from tests.agora_world import local
    bot = local("claude")
    alpha = svc_no_auth.access.resolve_scope(bot, "project:alpha", claim=False)
    assert svc_no_auth.access.can_read_scope(bot, alpha)
    assert svc_no_auth.contact_ok(bot, "anyone")


def test_every_write_is_audited_without_bodies(svc: AgoraService, ana: Participant) -> None:
    svc.profile_set(ana, description="secret-free text")
    _claim(svc, ana)
    svc.membership(ana, action="invite", project="project:alpha", participant="ben")
    ok, _ = svc.ctx.audit.verify()
    assert ok
    path = svc.ctx.audit._path
    with open(path, encoding="utf-8") as f:
        text = f.read()
    events = [line for line in text.splitlines() if line.strip()]
    assert len(events) >= 3
    assert "secret-free text" not in text
    assert '"agora.profile.set"' in text and '"agora.membership.invite"' in text
    assert '"agora.scope.claim"' in text
