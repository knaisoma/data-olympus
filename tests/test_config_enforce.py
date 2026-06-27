# tests/test_config_enforce.py
"""Config tests for enforcement settings."""
from __future__ import annotations

from data_olympus.config import load_config


def test_consult_ttl_default(monkeypatch) -> None:
    monkeypatch.delenv("KB_CONSULT_TTL_SEC", raising=False)
    cfg = load_config()
    assert cfg.consult_ttl_sec == 300


def test_consult_ttl_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KB_CONSULT_TTL_SEC", "120")
    cfg = load_config()
    assert cfg.consult_ttl_sec == 120
