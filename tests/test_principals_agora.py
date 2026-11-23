"""Principal kind, owner and Agora capabilities (issue #492, Task 2)."""
from __future__ import annotations

import pytest

from data_olympus.principals import (
    AGORA_IMPLICIT_CAPABILITIES,
    AGORA_TOOL_CAPABILITY,
    ALL_CAPABILITIES,
    ANONYMOUS,
    CAP_AGORA_ADMIN,
    CAP_AGORA_POST,
    CAP_AGORA_READ,
    LOCAL_TRUSTED,
    Principal,
    PrincipalRegistry,
)


def test_kind_and_owner_parsed() -> None:
    reg = PrincipalRegistry(principals=[
        {"name": "ana", "token": "t1", "kind": "person"},
        {"name": "ana-claude", "token": "t2", "owner": "ana"},
    ])
    ana = reg.get("ana")
    assert ana is not None and ana.kind == "person"
    agent = reg.get("ana-claude")
    assert agent is not None and agent.kind == "agent" and agent.owner == "ana"


def test_owner_may_be_listed_after_its_agent() -> None:
    reg = PrincipalRegistry(principals=[
        {"name": "ana-claude", "token": "t2", "kind": "agent", "owner": "ana"},
        {"name": "ana", "token": "t1", "kind": "person"},
    ])
    agent = reg.get("ana-claude")
    assert agent is not None and agent.owner == "ana"


def test_operator_may_own_agents() -> None:
    reg = PrincipalRegistry(auth_token="op", principals=[
        {"name": "op-bot", "token": "t2", "owner": "operator"},
    ])
    op = reg.get("operator")
    assert op is not None and op.kind == "person"
    assert op.capabilities == ALL_CAPABILITIES


@pytest.mark.parametrize("specs,match", [
    ([{"name": "x", "token": "t", "kind": "robot"}], "entry 1.*kind"),
    ([{"name": "x", "token": "t", "owner": "nobody"}], "owner 'nobody'"),
    ([{"name": "p", "token": "t", "kind": "person", "owner": "q"}], "only agents"),
    ([{"name": "a", "token": "t1"}, {"name": "b", "token": "t2", "owner": "a"}],
     "not a person"),
    ([{"name": "a", "token": "t1", "owner": "a"}], "own owner"),
    ([{"name": "a", "token": "t1", "owner": 5}], "entry 1.*owner"),
])
def test_bad_kind_or_owner_fails_startup(specs: list[dict], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        PrincipalRegistry(principals=specs)


def test_implicit_agora_caps_added_to_explicit_lists() -> None:
    reg = PrincipalRegistry(
        principals=[{"name": "r", "token": "t", "capabilities": ["read"]}],
        agora_implicit_caps=True,
    )
    r = reg.get("r")
    assert r is not None
    assert r.capabilities == {"read", "agora_read", "agora_post"}


def test_implicit_caps_off_and_admin_never_implicit() -> None:
    reg = PrincipalRegistry(principals=[{"name": "r", "token": "t"}],
                            agora_implicit_caps=False)
    r = reg.get("r")
    assert r is not None and not r.has("agora_read")
    reg2 = PrincipalRegistry(principals=[{"name": "r", "token": "t"}],
                             agora_implicit_caps=True)
    r2 = reg2.get("r")
    assert r2 is not None and not r2.has("agora_admin")


def test_explicit_agora_admin_is_kept() -> None:
    reg = PrincipalRegistry(principals=[
        {"name": "adm", "token": "t", "kind": "person",
         "capabilities": ["read", "agora_admin"]},
    ])
    adm = reg.get("adm")
    assert adm is not None and adm.has(CAP_AGORA_ADMIN)


def test_anonymous_has_no_agora_caps() -> None:
    assert not ANONYMOUS.has("agora_read")
    assert not ANONYMOUS.has("agora_post")


def test_local_trusted_is_a_person_with_agora_caps() -> None:
    assert LOCAL_TRUSTED.kind == "person"
    assert LOCAL_TRUSTED.has(CAP_AGORA_READ) and LOCAL_TRUSTED.has(CAP_AGORA_POST)


def test_error_never_echoes_token() -> None:
    with pytest.raises(ValueError) as e:
        PrincipalRegistry(principals=[
            {"name": "x", "token": "s3cr3t-tok", "kind": "robot"}])
    assert "s3cr3t-tok" not in str(e.value)
    with pytest.raises(ValueError) as e2:
        PrincipalRegistry(principals=[
            {"name": "x", "token": "s3cr3t-tok", "owner": "s3cr3t-tok"}])
    assert "s3cr3t-tok" not in str(e2.value).replace("owner 's3cr3t-tok'", "")


def test_principals_listing_and_unknown_get() -> None:
    reg = PrincipalRegistry(auth_token="op", principals=[{"name": "a", "token": "t"}])
    assert {p.name for p in reg.principals()} == {"operator", "a"}
    assert reg.get("missing") is None


def test_positional_construction_still_works() -> None:
    p = Principal("x", frozenset({"read"}), True)
    assert p.kind == "agent" and p.owner is None


def test_agora_caps_in_all_capabilities() -> None:
    assert {CAP_AGORA_READ, CAP_AGORA_POST, CAP_AGORA_ADMIN} <= ALL_CAPABILITIES
    assert {CAP_AGORA_READ, CAP_AGORA_POST} == AGORA_IMPLICIT_CAPABILITIES


def test_agora_tool_capability_map() -> None:
    reads = {"agora_inbox", "agora_thread_get", "agora_threads", "agora_search",
             "agora_profile_get", "agora_directory", "agora_scopes",
             "agora_mark_seen", "agora_catch_up", "agora_caught_up"}
    for name in reads:
        assert AGORA_TOOL_CAPABILITY[name] == CAP_AGORA_READ
    posts = set(AGORA_TOOL_CAPABILITY) - reads
    assert posts and all(AGORA_TOOL_CAPABILITY[n] == CAP_AGORA_POST for n in posts)
    assert len(AGORA_TOOL_CAPABILITY) == 28
