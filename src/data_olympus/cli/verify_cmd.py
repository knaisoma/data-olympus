"""`data-olympus verify`: automated pass/fail health + functional round-trip
checks against a running data-olympus instance. Used by hand and as the
pre/post-release verification gate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import argparse

# NOTE: declare every module-level import here in Task 1. Later tasks add
# functions only (no new top-level imports), so ruff's E402 never fires.
# `json` is used by run_verify (Task 4); the argparse annotations are strings.

_ALL_CHECKS = ("health", "readiness", "search", "enforcement")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    connection_error: bool = False


def check_health(client: httpx.Client) -> CheckResult:
    """GET /api/v1/health: pass on 200 with degraded=false (503 = degraded)."""
    try:
        resp = client.get("/api/v1/health")
    except httpx.HTTPError as exc:
        return CheckResult("health", False, f"request failed: {exc}", connection_error=True)
    if resp.status_code != 200:
        return CheckResult("health", False, f"status {resp.status_code} (degraded or down)")
    try:
        body = resp.json()
    except ValueError:
        return CheckResult("health", False, "non-JSON health body")
    if not isinstance(body, dict):
        return CheckResult("health", False, "unexpected non-object health body")
    if bool(body.get("degraded")):
        return CheckResult("health", False, "health reports degraded")
    return CheckResult("health", True, "healthy")


def check_readiness(client: httpx.Client) -> CheckResult:
    """GET /readyz: the k8s readiness probe target; pass on 200."""
    try:
        resp = client.get("/readyz")
    except httpx.HTTPError as exc:
        return CheckResult("readiness", False, f"request failed: {exc}", connection_error=True)
    ok = resp.status_code == 200
    return CheckResult("readiness", ok, "ready" if ok else f"status {resp.status_code}")


def check_search(client: httpx.Client, probe: str) -> CheckResult:
    """GET /api/v1/search: confirms the index answers reads with a hits list."""
    try:
        resp = client.get("/api/v1/search", params={"q": probe, "limit": 1})
    except httpx.HTTPError as exc:
        return CheckResult("search", False, f"request failed: {exc}", connection_error=True)
    if resp.status_code != 200:
        return CheckResult("search", False, f"status {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        return CheckResult("search", False, "non-JSON search body")
    if not isinstance(body, dict):
        return CheckResult("search", False, "unexpected non-object search body")
    hits = body.get("hits")
    if not isinstance(hits, list):
        return CheckResult("search", False, "response missing 'hits' list")
    return CheckResult("search", True, f"{len(hits)} hit(s) for probe {probe!r}")


def check_enforcement(client: httpx.Client, token: str | None = None) -> CheckResult:
    """Probe /api/v1/gate/check. Deployment-tolerant: 404 (routes not
    mounted) and 401/403 without a token are informational passes; a token
    that is rejected, or a 5xx, fail."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"workspace": "data-olympus", "session_id": "verify-probe"}
    try:
        resp = client.post(
            "/api/v1/gate/check", json=payload, headers=headers
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            "enforcement", False, f"request failed: {exc}", connection_error=True
        )
    code = resp.status_code
    if code == 200:
        return CheckResult("enforcement", True, "enforcement plane responding")
    if code == 404:
        return CheckResult(
            "enforcement", True, "enforcement routes not mounted (open deployment)"
        )
    if code in (401, 403):
        if token:
            return CheckResult(
                "enforcement", False, f"enforcement rejected the token ({code})"
            )
        return CheckResult(
            "enforcement",
            True,
            "enforcement active (auth required; pass --token to test round-trip)",
        )
    return CheckResult("enforcement", False, f"unexpected status {code}")


def run_verify(
    *,
    target: str,
    as_json: bool = False,
    timeout: float = 10.0,
    probe: str = "the",
    checks: list[str] | None = None,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> int:
    """Orchestrate selected checks and report results to stdout. Returns 0, 1,
    or 4."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(base_url=target.rstrip("/"), timeout=timeout)
    try:
        selected_checks = (
            checks if checks is not None else list(_ALL_CHECKS)
        )
        results = []
        for check_name in _ALL_CHECKS:
            if check_name not in selected_checks:
                continue
            if check_name == "health":
                results.append(check_health(client))
            elif check_name == "readiness":
                results.append(check_readiness(client))
            elif check_name == "search":
                results.append(check_search(client, probe))
            elif check_name == "enforcement":
                results.append(check_enforcement(client, token))
    finally:
        if owns_client:
            client.close()

    all_ok = all(r.ok for r in results)
    if as_json:
        print(
            json.dumps(
                {
                    "target": target,
                    "ok": all_ok,
                    "checks": [
                        {"name": r.name, "ok": r.ok, "detail": r.detail}
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        for r in results:
            mark = "PASS" if r.ok else "FAIL"
            print(f"{mark}  {r.name}: {r.detail}")
        print(
            f"{'ok' if all_ok else 'FAILED'}: "
            f"{sum(r.ok for r in results)}/{len(results)} checks passed "
            f"against {target}"
        )
    if all_ok:
        return 0
    if results and all(r.connection_error for r in results):
        return 1
    return 4


def add_verify_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire the verify subcommand into the argparse dispatcher."""
    p = sub.add_parser(
        "verify",
        help="run health + functional round-trip checks against a running instance",
    )
    p.add_argument(
        "--target",
        default=None,
        help="base URL (default: $KB_ENDPOINT or http://localhost:8080)",
    )
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-request timeout seconds",
    )
    p.add_argument("--probe", default="the", help="search probe term (default: the)")
    p.add_argument(
        "--checks",
        default=None,
        help="comma-separated subset to run (default: all): "
        "health,readiness,search,enforcement",
    )
    p.add_argument(
        "--token",
        default=None,
        help="bearer token for enforcement check (default: $KB_AUTH_TOKEN)",
    )
    p.set_defaults(func=_cmd_verify)


def _cmd_verify(args: argparse.Namespace) -> int:
    """Entry point for the verify subcommand; resolve target and call
    run_verify."""
    import os
    import sys

    target = args.target or os.environ.get("KB_ENDPOINT") or (
        "http://localhost:8080"
    )

    checks = None
    if args.checks:
        checks = [c.strip() for c in args.checks.split(",")]
        unknown = set(checks) - set(_ALL_CHECKS)
        if unknown:
            print(
                f"error: unknown check(s): {','.join(sorted(unknown))}",
                file=sys.stderr,
            )
            return 2

    token = args.token or os.environ.get("KB_AUTH_TOKEN")

    return run_verify(
        target=target,
        as_json=args.json,
        timeout=args.timeout,
        probe=args.probe,
        checks=checks,
        token=token,
    )
