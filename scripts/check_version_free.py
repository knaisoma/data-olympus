#!/usr/bin/env python3
"""Release guard: refuse to publish a version that already exists on any
external registry.

Once `vX.Y.Z` is published (PyPI, the ghcr `:vX.Y.Z` image tag, or a GitHub
release/tag) it is immutable. This is the FIRST step of the tag-release `decide`
job, before `git tag -a` / `git push origin`, so a duplicate never leaves an
orphaned public tag behind. It also runs in PR CI to catch a duplicate declared
version before merge.

Idempotency (see should_tag.py): should_tag.py resolves only LOCAL git
tags/commits, not registry digests. So a legitimate reconcile re-run is
identified HERE by checking whether the local tag `vX.Y.Z` already exists AND
points at the current HEAD commit. If it does, this exits 0 (allow) and does not
query any registry: the downstream image/release jobs are idempotent and must be
allowed to finish a partial release. If the version exists on a registry but no
local tag matches HEAD, this hard-fails.

Registry-outage behavior (operator-approved): FAIL CLOSED. An unreachable PyPI
or ghcr blocks the release rather than assuming the version is free. The
operator-only override env `KB_BYPASS_VERSION_CHECK=1` allows proceeding without
any registry query (also used to unblock a genuine registry outage).

CLI: `python3 scripts/check_version_free.py --version X.Y.Z [--repo knaisoma/data-olympus]`
Exit 0 = free (or idempotent reconcile, or operator bypass); 3 = version taken
somewhere; 4 = a registry was unreachable (fail closed).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BYPASS_ENV = "KB_BYPASS_VERSION_CHECK"


class VersionTaken(Exception):
    """The version already exists on at least one external registry."""

    exit_code = 3


class RegistryError(Exception):
    """A registry could not be reached; fail closed."""

    exit_code = 4


def evaluate(
    *,
    version: str,
    on_pypi: bool,
    on_ghcr: bool,
    on_github: bool,
) -> tuple[int, str]:
    """Return (exit_code, report). 0 = free everywhere; VersionTaken otherwise."""
    where = [
        name
        for name, present in (("PyPI", on_pypi), ("ghcr", on_ghcr), ("GitHub", on_github))
        if present
    ]
    if not where:
        return 0, f"version {version} is free: not on PyPI, ghcr, or GitHub"
    report = (
        f"version {version} already published and is immutable; found on: "
        f"{', '.join(where)}. Bump the version or set {BYPASS_ENV}=1 to override."
    )
    return VersionTaken.exit_code, report


def idempotent_rerun(version: str, tag_commit: str | None, head_commit: str) -> bool:
    """True when local tag vX.Y.Z exists AND points at the current HEAD commit.

    That is the reconcile case: a prior run already created and pushed the tag at
    this commit, so re-running the idempotent downstream jobs is legitimate and
    the registry check is skipped. `tag_commit` is None when the tag does not
    exist locally (or could not be resolved).
    """
    del version  # signature kept parallel to should_tag.release_action
    return tag_commit is not None and tag_commit == head_commit


def _on_pypi(version: str) -> bool:
    """True if data-olympus==version is published on PyPI.

    404 = free; 200 = taken; any other outcome raises RegistryError (fail closed).
    """
    url = f"https://pypi.org/pypi/data-olympus/{version}/json"
    req = urllib.request.Request(url, method="GET")  # noqa: S310 (fixed https host)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return bool(resp.status == 200)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise RegistryError(f"PyPI query for {version} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(f"PyPI unreachable for {version}: {exc}") from exc


def _gh_json(path: str) -> object:
    """Call `gh api <path>` and return parsed JSON, or raise RegistryError.

    A 404 is signalled by returning None so callers can treat "no such package"
    as free rather than an outage.
    """
    out = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        if "404" in out.stderr or "Not Found" in out.stderr:
            return None
        raise RegistryError(f"gh api {path} failed: {out.stderr.strip()}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"gh api {path} returned non-JSON: {exc}") from exc


def _on_ghcr(version: str, repo: str) -> bool:
    """True if the ghcr image tag :vX.Y.Z exists for the org's container package."""
    org = repo.split("/", 1)[0]
    versions = _gh_json(
        f"/orgs/{org}/packages/container/data-olympus/versions?per_page=100"
    )
    if versions is None:
        # Package does not exist yet: the version cannot be published there.
        return False
    if not isinstance(versions, list):
        raise RegistryError("unexpected ghcr package versions payload")
    wanted = f"v{version}"
    for entry in versions:
        tags = (((entry or {}).get("metadata") or {}).get("container") or {}).get("tags") or []
        if wanted in tags:
            return True
    return False


def _on_github(version: str, repo: str) -> bool:
    """True if a GitHub release OR a git ref exists for tag vX.Y.Z on `repo`."""
    tag = f"v{version}"
    release = _gh_json(f"/repos/{repo}/releases/tags/{tag}")
    if release is not None:
        return True
    ref = _gh_json(f"/repos/{repo}/git/ref/tags/{tag}")
    return ref is not None


def _tag_commit(tag: str) -> str | None:
    """Return the commit the local tag points at (dereferenced), or None."""
    out = subprocess.run(
        ["git", "rev-list", "-n", "1", tag], capture_output=True, text=True
    )
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


def _head_commit() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_version_free")
    parser.add_argument("--version", required=True, help="target version X.Y.Z")
    parser.add_argument("--repo", default="knaisoma/data-olympus")
    args = parser.parse_args(argv)
    version = args.version

    if os.environ.get(BYPASS_ENV) == "1":
        print(
            f"{BYPASS_ENV}=1 set: skipping version-free check for {version} "
            "(operator override)"
        )
        return 0

    # Idempotent reconcile: a local tag already pins vX.Y.Z at this HEAD commit.
    tag_commit = _tag_commit(f"v{version}")
    if idempotent_rerun(version, tag_commit, _head_commit()):
        print(
            f"local tag v{version} already points at HEAD: idempotent reconcile, "
            "skipping registry checks (allow)"
        )
        return 0

    try:
        on_pypi = _on_pypi(version)
        on_ghcr = _on_ghcr(version, args.repo)
        on_github = _on_github(version, args.repo)
    except RegistryError as exc:
        print(
            f"registry check failed closed for {version}: {exc}. "
            f"Set {BYPASS_ENV}=1 to override once the registry is reachable.",
            file=sys.stderr,
        )
        return RegistryError.exit_code

    code, report = evaluate(
        version=version,
        on_pypi=on_pypi,
        on_ghcr=on_ghcr,
        on_github=on_github,
    )
    print(report, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
