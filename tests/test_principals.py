"""Unit tests for the Principal / PrincipalRegistry identity+capability model."""
from __future__ import annotations

from data_olympus.principals import (
    ALL_CAPABILITIES,
    ANONYMOUS,
    CAP_AUTO_COMMIT,
    CAP_PROPOSE,
    CAP_RESOLVE,
    LOCAL_TRUSTED,
    PrincipalRegistry,
    parse_principals_env,
)

TOKEN = "operator-secret"


def test_no_auth_configured_resolves_local_trusted() -> None:
    reg = PrincipalRegistry()
    assert reg.auth_configured is False
    p = reg.resolve(None)
    assert p is LOCAL_TRUSTED
    assert p.can_auto_commit is True
    assert p.has(CAP_PROPOSE)


def test_auth_token_maps_to_full_operator() -> None:
    reg = PrincipalRegistry(auth_token=TOKEN)
    assert reg.auth_configured is True
    p = reg.resolve(f"Bearer {TOKEN}")
    assert p.name == "operator"
    assert p.capabilities == ALL_CAPABILITIES
    assert p.authenticated is True


def test_missing_or_wrong_token_is_anonymous_when_configured() -> None:
    reg = PrincipalRegistry(auth_token=TOKEN)
    assert reg.resolve(None) is ANONYMOUS
    assert reg.resolve("Bearer nope") is ANONYMOUS
    assert reg.resolve("Basic xyz") is ANONYMOUS
    # Anonymous can read but cannot propose/auto-commit.
    assert not ANONYMOUS.has(CAP_PROPOSE)
    assert not ANONYMOUS.can_auto_commit


def test_per_agent_principal_capabilities() -> None:
    reg = PrincipalRegistry(
        auth_token=TOKEN,
        principals=[
            {"name": "proposer", "token": "ptok",
             "capabilities": ["read", "propose"]},
            {"name": "resolver", "token": "rtok",
             "capabilities": ["read", "propose", "resolve", "auto_commit"]},
        ],
    )
    proposer = reg.resolve("Bearer ptok")
    assert proposer.name == "proposer"
    assert proposer.has(CAP_PROPOSE)
    assert not proposer.can_auto_commit  # clamp: writes go to pending
    resolver = reg.resolve("Bearer rtok")
    assert resolver.has(CAP_RESOLVE)
    assert resolver.has(CAP_AUTO_COMMIT)


def test_principal_omitting_capabilities_defaults_to_least_privilege() -> None:
    """item 5: a KB_AUTH_PRINCIPALS entry with no explicit capabilities gets
    read+propose ONLY, never resolve/auto_commit. Otherwise an agent could
    approve its own proposals."""
    from data_olympus.principals import (
        CAP_BOOTSTRAP,
        CAP_READ,
        DEFAULT_PRINCIPAL_CAPABILITIES,
    )
    reg = PrincipalRegistry(
        auth_token=TOKEN,
        principals=[{"name": "agent", "token": "atok"}],  # no "capabilities" key
    )
    p = reg.resolve("Bearer atok")
    assert p.name == "agent"
    assert p.capabilities == DEFAULT_PRINCIPAL_CAPABILITIES
    assert p.has(CAP_READ)
    assert p.has(CAP_PROPOSE)
    # The dangerous capabilities must NOT be granted by default.
    assert not p.has(CAP_RESOLVE)
    assert not p.has(CAP_AUTO_COMMIT)
    assert not p.has(CAP_BOOTSTRAP)
    assert not p.can_auto_commit


def test_operator_token_still_gets_all_capabilities() -> None:
    """The back-compat single KB_AUTH_TOKEN operator keeps full capabilities;
    the least-privilege default applies only to KB_AUTH_PRINCIPALS entries."""
    reg = PrincipalRegistry(auth_token=TOKEN)
    p = reg.resolve(f"Bearer {TOKEN}")
    assert p.capabilities == ALL_CAPABILITIES
    assert p.has(CAP_RESOLVE)
    assert p.can_auto_commit


def test_principal_without_token_is_skipped() -> None:
    reg = PrincipalRegistry(principals=[{"name": "x", "capabilities": ["read"]}])
    # No token => no usable principal => auth not configured.
    assert reg.auth_configured is False


def test_parse_principals_env_tolerates_garbage() -> None:
    assert parse_principals_env("") == []
    assert parse_principals_env("not json") == []
    assert parse_principals_env('{"not": "a list"}') == []
    assert parse_principals_env('[{"name":"a","token":"t"}]') == [
        {"name": "a", "token": "t"}
    ]
