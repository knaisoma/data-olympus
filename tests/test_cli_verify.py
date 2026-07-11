from __future__ import annotations

import json

import httpx

from data_olympus.cli.main import build_parser
from data_olympus.cli.verify_cmd import (
    CheckResult,
    check_health,
    check_readiness,
    check_search,
    run_verify,
)


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
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="not ready")

    r = check_readiness(_client(handler))
    assert r.ok is False
    assert r.name == "readiness"


def test_check_search_ok_with_hits_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search"
        assert request.url.params.get("q") == "the"
        return httpx.Response(200, json={"hits": [], "total_returned": 0})

    assert check_search(_client(handler), "the").ok is True


def test_check_search_fails_when_hits_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": 1})

    r = check_search(_client(handler), "the")
    assert r.ok is False
    assert r.name == "search"


def test_check_search_fails_when_hits_not_a_list() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": "nope"})

    r = check_search(_client(handler), "the")
    assert r.ok is False


def _all_ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/health":
        return httpx.Response(200, json={"degraded": False})
    if request.url.path == "/readyz":
        return httpx.Response(200, text="ok")
    if request.url.path == "/api/v1/search":
        return httpx.Response(200, json={"hits": []})
    return httpx.Response(404)


def test_run_verify_returns_0_when_all_pass() -> None:
    rc = run_verify(target="http://kb.test", client=_client(_all_ok_handler))
    assert rc == 0


def test_run_verify_returns_4_when_a_check_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return httpx.Response(503, json={"degraded": True})
        return _all_ok_handler(request)

    rc = run_verify(target="http://kb.test", client=_client(handler))
    assert rc == 4


def test_run_verify_json_output_lists_checks(capsys) -> None:
    rc = run_verify(
        target="http://kb.test", as_json=True, client=_client(_all_ok_handler)
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert {c["name"] for c in out["checks"]} == {"health", "readiness", "search"}


def test_cli_parses_verify_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(["verify", "--target", "http://kb.test", "--json"])
    assert args.command == "verify"
    assert args.target == "http://kb.test"
    assert args.json is True
    assert callable(args.func)


def _unreachable_client() -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.Client(base_url="http://kb.test", transport=httpx.MockTransport(handler))


def test_check_health_marks_connection_error() -> None:
    r = check_health(_unreachable_client())
    assert r.ok is False
    assert r.connection_error is True


def test_run_verify_returns_1_when_target_unreachable() -> None:
    assert run_verify(target="http://kb.test", client=_unreachable_client()) == 1


def test_run_verify_returns_4_when_some_but_not_all_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            # a real failure, not a connection error
            return httpx.Response(503, json={"degraded": True})
        if request.url.path == "/readyz":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={"hits": []})

    client = httpx.Client(base_url="http://kb.test", transport=httpx.MockTransport(handler))
    assert run_verify(target="http://kb.test", client=client) == 4


def test_run_verify_checks_selector_runs_only_selected(capsys) -> None:
    # readiness would fail (503) but we only run health+search, which pass.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(503, text="down")
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"degraded": False})
        return httpx.Response(200, json={"hits": []})

    client = httpx.Client(base_url="http://kb.test", transport=httpx.MockTransport(handler))
    rc = run_verify(target="http://kb.test", checks=["health", "search"], as_json=True,
                    client=client)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert {c["name"] for c in out["checks"]} == {"health", "search"}


def test_cli_rejects_unknown_check_name() -> None:
    parser = build_parser()
    args = parser.parse_args(["verify", "--checks", "health,bogus"])
    from data_olympus.cli.verify_cmd import _cmd_verify

    assert _cmd_verify(args) == 2
