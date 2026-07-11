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


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_health(client: httpx.Client) -> CheckResult:
    """GET /api/v1/health: pass on 200 with degraded=false (503 = degraded)."""
    try:
        resp = client.get("/api/v1/health")
    except httpx.HTTPError as exc:
        return CheckResult("health", False, f"request failed: {exc}")
    if resp.status_code != 200:
        return CheckResult("health", False, f"status {resp.status_code} (degraded or down)")
    try:
        degraded = bool(resp.json().get("degraded"))
    except ValueError:
        return CheckResult("health", False, "non-JSON health body")
    if degraded:
        return CheckResult("health", False, "health reports degraded")
    return CheckResult("health", True, "healthy")


def check_readiness(client: httpx.Client) -> CheckResult:
    """GET /readyz: the k8s readiness probe target; pass on 200."""
    try:
        resp = client.get("/readyz")
    except httpx.HTTPError as exc:
        return CheckResult("readiness", False, f"request failed: {exc}")
    ok = resp.status_code == 200
    return CheckResult("readiness", ok, "ready" if ok else f"status {resp.status_code}")
