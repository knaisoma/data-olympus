from __future__ import annotations

import json

import httpx

from data_olympus.cli.verify_cmd import CheckResult, check_health


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://kb.test", transport=httpx.MockTransport(handler))


def test_check_health_ok_when_200_and_not_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health"
        return httpx.Response(200, json={"degraded": False, "kb_commit": "abc"})

    result = check_health(_client(handler))
    assert isinstance(result, CheckResult)
    assert result.name == "health"
    assert result.ok is True


def test_check_health_fails_on_503_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"degraded": True})

    result = check_health(_client(handler))
    assert result.ok is False
    assert "degraded" in result.detail.lower() or "503" in result.detail
