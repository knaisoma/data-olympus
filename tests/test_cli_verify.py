from __future__ import annotations

import json

import httpx

from data_olympus.cli.verify_cmd import CheckResult, check_health, check_readiness


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
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"degraded": True})

    result = check_health(_client(handler))
    assert result.ok is False
    assert "degraded" in result.detail.lower() or "503" in result.detail


def test_check_readiness_ok_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/readyz"
        return httpx.Response(200, text="ok")

    assert check_readiness(_client(handler)).ok is True


def test_check_readiness_fails_on_503() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # unused arg: _ prefix satisfies ruff ARG001
        return httpx.Response(503, text="not ready")

    r = check_readiness(_client(handler))
    assert r.ok is False
    assert r.name == "readiness"


from data_olympus.cli.verify_cmd import check_search


def test_check_search_ok_with_hits_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search"
        assert request.url.params.get("q") == "the"
        return httpx.Response(200, json={"hits": [], "total_returned": 0})

    assert check_search(_client(handler), "the").ok is True


def test_check_search_fails_when_hits_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # unused arg: _ prefix satisfies ruff ARG001
        return httpx.Response(200, json={"unexpected": 1})

    r = check_search(_client(handler), "the")
    assert r.ok is False
    assert r.name == "search"
