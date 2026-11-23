"""Agora settings (issue #492, phase 1 Task 1)."""
from __future__ import annotations

import os

import pytest

from data_olympus.config import load_config


@pytest.fixture(autouse=True)
def _clean_agora_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [k for k in os.environ if k.startswith("KB_AGORA_")]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("KB_CONFIDENCE_THRESHOLD", raising=False)


def test_agora_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "0.9")
    cfg = load_config()
    assert cfg.agora_enabled is False
    assert cfg.agora_state_path == "/state/agora/agora.db"
    assert cfg.agora_audit_log_path == "/state/agora/audit.log"
    assert cfg.agora_implicit_caps is True and cfg.agora_agent_votes is True
    assert cfg.agora_agent_vote_threshold == 0.9  # follows KB_CONFIDENCE_THRESHOLD
    assert cfg.agora_max_body_bytes == 32768
    assert cfg.agora_rate_limit_per_hour == 300
    assert cfg.agora_decision_timeout_sec == 604800


def test_agora_threshold_default_without_kb_threshold() -> None:
    assert load_config().agora_agent_vote_threshold == 0.85


def test_agora_explicit_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_AGORA_ENABLED", "on")
    monkeypatch.setenv("KB_AGORA_STATE_PATH", "/data/agora.db")
    monkeypatch.setenv("KB_AGORA_AUDIT_LOG_PATH", "/data/audit.log")
    monkeypatch.setenv("KB_AGORA_IMPLICIT_CAPS", "off")
    monkeypatch.setenv("KB_AGORA_LOCAL_PERSON", "aj.dev")
    monkeypatch.setenv("KB_AGORA_AGENT_VOTES", "no")
    monkeypatch.setenv("KB_AGORA_AGENT_VOTE_THRESHOLD", "0.5")
    monkeypatch.setenv("KB_AGORA_MAX_BODY_BYTES", "2048")
    monkeypatch.setenv("KB_AGORA_RATE_LIMIT_PER_HOUR", "0")
    monkeypatch.setenv("KB_AGORA_DECISION_TIMEOUT_SEC", "3600")
    cfg = load_config()
    assert cfg.agora_enabled is True
    assert cfg.agora_state_path == "/data/agora.db"
    assert cfg.agora_audit_log_path == "/data/audit.log"
    assert cfg.agora_implicit_caps is False
    assert cfg.agora_local_person == "aj.dev"
    assert cfg.agora_agent_votes is False
    assert cfg.agora_agent_vote_threshold == 0.5
    assert cfg.agora_max_body_bytes == 2048
    assert cfg.agora_rate_limit_per_hour == 0
    assert cfg.agora_decision_timeout_sec == 3600


def test_agora_local_person_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "bad name!")
    assert load_config().agora_local_person == "local"


def test_agora_local_person_falls_back_when_getuser_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> str:
        raise OSError("no user")

    monkeypatch.setattr("getpass.getuser", boom)
    assert load_config().agora_local_person == "local"


def test_agora_local_person_uses_os_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "ajdelaguila")
    assert load_config().agora_local_person == "ajdelaguila"


@pytest.mark.parametrize("var,value", [
    ("KB_AGORA_ENABLED", "maybe"),
    ("KB_AGORA_IMPLICIT_CAPS", "sure"),
    ("KB_AGORA_AGENT_VOTES", "2"),
    ("KB_AGORA_AGENT_VOTE_THRESHOLD", "1.5"),
    ("KB_AGORA_AGENT_VOTE_THRESHOLD", "abc"),
    ("KB_AGORA_MAX_BODY_BYTES", "100"),
    ("KB_AGORA_MAX_BODY_BYTES", "lots"),
    ("KB_AGORA_LOCAL_PERSON", "has space"),
    ("KB_AGORA_DECISION_TIMEOUT_SEC", "60"),
    ("KB_AGORA_DECISION_TIMEOUT_SEC", "7776001"),
    ("KB_AGORA_RATE_LIMIT_PER_HOUR", "-1"),
])
def test_agora_rejects_bad_values(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str,
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=var):
        load_config()
