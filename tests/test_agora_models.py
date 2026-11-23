"""Agora models, scopes and participant resolution (issue #492, Task 3)."""
from __future__ import annotations

import pytest

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.identity import resolve_participant
from data_olympus.agora.models import Participant, ParticipantRecord, Profile, new_id
from data_olympus.agora.scopes import parse_scope
from data_olympus.principals import ANONYMOUS, LOCAL_TRUSTED, Principal


@pytest.mark.parametrize("raw,tier,project,component,stack", [
    ("universal", "T1", None, None, None),
    ("stack:python", "T2", None, None, "python"),
    ("project:data-olympus", "T3", "data-olympus", None, None),
    ("component:data-olympus/mcp", "T4", "data-olympus", "mcp", None),
])
def test_parse_scope(raw: str, tier: str, project: str | None,
                     component: str | None, stack: str | None) -> None:
    ref = parse_scope(raw)
    assert (ref.id, ref.tier, ref.project, ref.component, ref.stack) == (
        raw, tier, project, component, stack)


@pytest.mark.parametrize("raw", [
    "", "project:", "project:a/b", "component:a", "stack:../x", "team:x",
    "project:" + "a" * 65, "component:a/", "component:/b", "Universal",
    " project:a", "project:-a", "stack:a b",
])
def test_parse_scope_rejects(raw: str) -> None:
    with pytest.raises(AgoraError) as e:
        parse_scope(raw)
    assert e.value.code == "invalid"


def test_error_str_carries_code() -> None:
    err = AgoraError("not_found", "no such thread")
    assert str(err) == "not_found: no such thread"
    with pytest.raises(AgoraError, match="not_found"):
        raise err


def test_error_rejects_unknown_code() -> None:
    with pytest.raises(ValueError):
        AgoraError("weird", "x")


def test_resolve_auth_on_uses_principal_not_self_declaration() -> None:
    p = Principal("ana-claude", frozenset({"agora_read"}), True, kind="agent", owner="ana")
    part = resolve_participant(p, agent_identity="pretend-admin", auth_on=True,
                               local_person="me")
    assert (part.name, part.owner, part.verified, part.via, part.admin) == (
        "ana-claude", "ana", True, "pretend-admin", False)


def test_resolve_auth_on_admin_and_via_truncated() -> None:
    p = Principal("boss", frozenset({"agora_admin"}), True, kind="person")
    part = resolve_participant(p, agent_identity="x" * 100, auth_on=True,
                               local_person="me")
    assert part.admin is True and part.kind == "person" and len(part.via) == 64


def test_resolve_auth_on_unauthenticated_forbidden() -> None:
    with pytest.raises(AgoraError) as e:
        resolve_participant(ANONYMOUS, agent_identity="", auth_on=True, local_person="me")
    assert e.value.code == "forbidden"


def test_resolve_no_auth_local_person_and_agent() -> None:
    me = resolve_participant(LOCAL_TRUSTED, agent_identity="", auth_on=False,
                             local_person="aj")
    assert (me.name, me.kind, me.verified, me.admin) == ("aj", "person", False, True)
    same = resolve_participant(LOCAL_TRUSTED, agent_identity="aj", auth_on=False,
                               local_person="aj")
    assert same.kind == "person"
    bot = resolve_participant(LOCAL_TRUSTED, agent_identity="claude", auth_on=False,
                              local_person="aj")
    assert (bot.name, bot.kind, bot.owner, bot.verified, bot.admin) == (
        "claude", "agent", "aj", False, False)


def test_resolve_no_auth_rejects_bad_identity() -> None:
    with pytest.raises(AgoraError) as e:
        resolve_participant(LOCAL_TRUSTED, agent_identity="bad name!", auth_on=False,
                            local_person="aj")
    assert e.value.code == "invalid"


def test_participant_properties() -> None:
    rogue = Participant("rogue", "agent", None, verified=True)
    assert rogue.is_autonomous and rogue.accountable == "rogue"
    owned = Participant("ana-claude", "agent", "ana", verified=True)
    assert not owned.is_autonomous and owned.accountable == "ana"
    person = Participant("ana", "person", None, verified=True)
    assert not person.is_autonomous and person.accountable == "ana"


def test_agent_card_shape() -> None:
    record = ParticipantRecord("ana-claude", "agent", "ana", True, 1.0, 2.0)
    card = Profile("ana-claude", "Backend dev", ("python", "testing"),
                   tool="claude-code").to_agent_card(record, {"project:x": "reply"})
    assert card["name"] == "ana-claude" and card["description"] == "Backend dev"
    assert card["skills"][0] == {"id": "python", "name": "python", "tags": ["python"]}
    assert card["x-agora"]["owner"] == "ana"
    assert card["x-agora"]["kind"] == "agent"
    assert card["x-agora"]["tool"] == "claude-code"
    assert card["x-agora"]["autonomy"] == {"project:x": "reply"}


def test_new_id_prefix() -> None:
    a, b = new_id("th"), new_id("th")
    assert a.startswith("th_") and len(a) == 27 and a != b
