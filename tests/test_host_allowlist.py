"""KNA-70 (gh #139): first-class reverse-proxy Host-header allowlist.

The core deliverable is the regression test that was the CI blind spot letting
v0.4.1 ship broken: boot the app with a configured public hostname, confirm a
request carrying that Host header succeeds and a foreign Host still gets 421.
This is the exact 2026-07-09 kn-dev outage shape reproduced in CI.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
import pytest

import data_olympus.server as server
from data_olympus.config import load_config

if TYPE_CHECKING:
    from pathlib import Path


_PUBLIC_HOST = "data-olympus-mcp.data-olympus.apps.172.30.1.2.nip.io"


def test_public_hostnames_config_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_PUBLIC_HOSTNAMES", f"{_PUBLIC_HOST}, kb.example.com")

    cfg = load_config()
    assert cfg.public_hostnames == [_PUBLIC_HOST, "kb.example.com"]


def test_resolve_allowed_hosts_none_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty knob must resolve to None so http_app keeps its own settings default
    (passing [] would flip fastmcp into strict-with-no-host-allowed -> 421 all)."""
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    # No FASTMCP_HTTP_ALLOWED_HOSTS either.
    import fastmcp

    monkeypatch.setattr(fastmcp.settings, "http_allowed_hosts", None, raising=False)
    cfg = load_config()
    assert server._resolve_allowed_hosts(cfg) is None


def test_resolve_allowed_hosts_merges_fastmcp_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_PUBLIC_HOSTNAMES", _PUBLIC_HOST)
    import fastmcp

    monkeypatch.setattr(
        fastmcp.settings, "http_allowed_hosts", ["legacy.example.com"], raising=False
    )
    cfg = load_config()
    resolved = server._resolve_allowed_hosts(cfg)
    assert resolved == [_PUBLIC_HOST, "legacy.example.com"]


def test_warn_when_protected_public_bind_without_allowed_host(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The silent-breakage shape: protection on + non-loopback bind + no host."""
    import fastmcp

    monkeypatch.setattr(
        fastmcp.settings, "http_host_origin_protection", True, raising=False
    )
    with caplog.at_level(logging.WARNING):
        server._warn_if_unprotected_bind(bind_host="0.0.0.0", allowed_hosts=None)
    assert any("421" in r.message for r in caplog.records)


def test_no_warn_when_public_hostname_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import fastmcp

    monkeypatch.setattr(
        fastmcp.settings, "http_host_origin_protection", True, raising=False
    )
    with caplog.at_level(logging.WARNING):
        server._warn_if_unprotected_bind(
            bind_host="0.0.0.0", allowed_hosts=[_PUBLIC_HOST]
        )
    assert not any("421" in r.message for r in caplog.records)


def test_no_warn_when_protection_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import fastmcp

    monkeypatch.setattr(
        fastmcp.settings, "http_host_origin_protection", False, raising=False
    )
    with caplog.at_level(logging.WARNING):
        server._warn_if_unprotected_bind(bind_host="0.0.0.0", allowed_hosts=None)
    assert not any("421" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_configured_public_host_succeeds_foreign_host_gets_421(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core regression (the CI blind spot that let v0.4.1 ship broken).

    Boot the real app with KB_PUBLIC_HOSTNAMES configured, turn host protection
    on (the production posture that caused the outage), and confirm:
      - a request with the configured Host header -> not 421 (reaches the route)
      - a request with a foreign Host header     -> 421 Misdirected Request
    """
    import fastmcp

    monkeypatch.setattr(
        fastmcp.settings, "http_host_origin_protection", True, raising=False
    )
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_git_kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_PUBLIC_HOSTNAMES", _PUBLIC_HOST)

    cfg = load_config()
    app = server.build_app_from_config(cfg, bootstrap_now=True)
    allowed_hosts = server._resolve_allowed_hosts(cfg)
    assert allowed_hosts == [_PUBLIC_HOST]
    http_app = app.http_app(
        transport="streamable-http",
        allowed_hosts=allowed_hosts,
    )

    transport = httpx.ASGITransport(app=http_app)
    async with (
        http_app.router.lifespan_context({}),
        httpx.AsyncClient(
            transport=transport, base_url=f"http://{_PUBLIC_HOST}"
        ) as client,
    ):
        ok = await client.get("/api/v1/health", headers={"host": _PUBLIC_HOST})
        foreign = await client.get(
            "/api/v1/health", headers={"host": "evil.attacker.example.com"}
        )

    assert ok.status_code != 421, (
        f"configured public host was rejected: {ok.status_code} {ok.text}"
    )
    assert foreign.status_code == 421, (
        f"foreign host should be 421, got {foreign.status_code}"
    )
