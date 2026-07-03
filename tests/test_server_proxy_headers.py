"""Tests for uvicorn proxy-header configuration (WP3a item 3).

The rate limiter keys on the client remote_addr. Behind an ingress, uvicorn must
be told to trust X-Forwarded-For ONLY from configured proxies, otherwise either
(a) every client collapses to the proxy IP (proxy_headers off) or (b) a direct
client can spoof its IP (forwarded_allow_ips too broad). These tests pin the
off-by-default-safe behaviour and the configured behaviour.
"""
from __future__ import annotations

from data_olympus.config import load_config
from data_olympus.server import _uvicorn_proxy_kwargs


def test_proxy_headers_off_by_default() -> None:
    kwargs = _uvicorn_proxy_kwargs([])
    assert kwargs == {"proxy_headers": False}


def test_proxy_headers_enabled_for_trusted_proxies() -> None:
    kwargs = _uvicorn_proxy_kwargs(["10.0.0.1", "10.0.0.2"])
    assert kwargs["proxy_headers"] is True
    # uvicorn wants a comma-separated string, not a list.
    assert kwargs["forwarded_allow_ips"] == "10.0.0.1,10.0.0.2"


def test_proxy_headers_wildcard() -> None:
    kwargs = _uvicorn_proxy_kwargs(["*"])
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "*"


def test_trusted_proxies_config_default_empty(monkeypatch, tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.delenv("KB_TRUSTED_PROXIES", raising=False)
    cfg = load_config()
    assert cfg.trusted_proxies == []
    assert _uvicorn_proxy_kwargs(cfg.trusted_proxies) == {"proxy_headers": False}


def test_trusted_proxies_config_parsed(monkeypatch, tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_TRUSTED_PROXIES", "192.168.1.10, 192.168.1.11")
    cfg = load_config()
    assert cfg.trusted_proxies == ["192.168.1.10", "192.168.1.11"]


def test_audit_max_bytes_config(monkeypatch, tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_AUDIT_MAX_BYTES", "1048576")
    cfg = load_config()
    assert cfg.audit_max_bytes == 1048576
