#!/usr/bin/env python3
"""Release version-freshness gate: fail unless a target version is absent from
every external registry (PyPI, ghcr, GitHub releases and Git tags), fail-closed.

A version is only "free" (safe to publish) when it is confirmed absent from
all three registries AND every registry was reachable. If any registry could
not be queried, the version is treated as NOT free: never assume free during
an outage, since republishing an already-taken (immutable) version is
forbidden.

CLI: `python3 scripts/version_free.py --version X.Y.Z [--package data-olympus]
[--repo knaisoma/data-olympus] [--json]`
Exit 0 = free (safe to publish), 1 = taken or unreachable.

Intentionally separate from scripts/check_version_free.py. That script is the
tag-release pipeline guard: it has idempotent-reconcile and operator-bypass
allowances (it can exit 0 to allow a reconcile even when the version is already
taken), so its answer is 'is it safe for the pipeline to proceed', not 'is this
version free'. This module is the Release Manager's typed-evidence adapter: a
pure evaluate() with strict fail-closed semantics and JSON output, answering
only 'is this version absent from every registry'. Do not merge the two: the
pipeline guard's reconcile/bypass allowances would corrupt release-readiness
evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import cast

_STABLE_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_CANDIDATE_RE = re.compile(
    r"([0-9]+\.[0-9]+\.[0-9]+)-(hotfix\.)?rc\.(0|[1-9][0-9]*)"
)


@dataclass(frozen=True, slots=True)
class RegistryVersions:
    pypi: str
    ghcr: str
    github: str


def registry_versions(version: str) -> RegistryVersions:
    """Map one public release version to each registry's spelling."""
    if _STABLE_RE.fullmatch(version):
        tag = f"v{version}"
        return RegistryVersions(pypi=version, ghcr=tag, github=tag)
    candidate = _CANDIDATE_RE.fullmatch(version)
    if candidate:
        base, hotfix, number = candidate.groups()
        return RegistryVersions(
            pypi=f"{base}{'.dev' if hotfix else 'rc'}{number}",
            ghcr=version,
            github=version,
        )
    raise ValueError("version must be X.Y.Z, X.Y.Z-rc.N, or X.Y.Z-hotfix.rc.N")


def evaluate(
    pypi_present: bool | None,
    ghcr_present: bool | None,
    gh_release_present: bool | None,
    gh_tag_present: bool | None,
) -> dict[str, object]:
    """PURE. Each arg is True (found/taken), False (confirmed absent), or
    None (unreachable/unknown). A version is free only if all checks are
    exactly False; any True means taken, any None means unreachable and
    fails closed (not free)."""
    checks = (
        ("pypi", pypi_present),
        ("ghcr", ghcr_present),
        ("github_release", gh_release_present),
        ("github_tag", gh_tag_present),
    )
    unreachable = [name for name, present in checks if present is None]
    free = all(present is False for _, present in checks)
    result: dict[str, object] = {
        "pypi_taken": pypi_present,
        "ghcr_taken": ghcr_present,
        "github_release_taken": gh_release_present,
        "github_tag_taken": gh_tag_present,
        "unreachable": unreachable,
        "free": free,
    }
    return result


def _pypi_present(version: str, package: str = "data-olympus") -> bool | None:
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            status: int = resp.status
            return status == 200
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        return None
    except OSError:
        return None


def _ghcr_present(tag: str, package: str = "data-olympus") -> bool | None:
    reference = f"ghcr.io/knaisoma/{package}:{tag}"
    try:
        out = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", reference],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0:
        return True
    diagnostic = f"{out.stdout}\n{out.stderr}".lower()
    if (
        "manifest unknown" in diagnostic
        or "no such manifest" in diagnostic
        or f"{reference.lower()}: not found" in diagnostic
    ):
        return False
    return None


def _gh_release_present(tag: str, repo: str = "knaisoma/data-olympus") -> bool | None:
    return _gh_present(f"repos/{repo}/releases/tags/{tag}")


def _gh_tag_present(tag: str, repo: str = "knaisoma/data-olympus") -> bool | None:
    return _gh_present(f"repos/{repo}/git/ref/tags/{tag}")


def _gh_present(path: str) -> bool | None:
    try:
        out = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0:
        return True
    if "404" in out.stderr or "Not Found" in out.stderr:
        return False
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="version_free")
    parser.add_argument("--version", required=True, help="target version, e.g. 1.2.3")
    parser.add_argument("--package", default="data-olympus")
    parser.add_argument("--repo", default="knaisoma/data-olympus")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a summary")
    args = parser.parse_args(argv)

    try:
        versions = registry_versions(args.version)
    except ValueError as exc:
        print(f"invalid release version {args.version!r}: {exc}", file=sys.stderr)
        return 1

    pypi = _pypi_present(versions.pypi, args.package)
    ghcr = _ghcr_present(versions.ghcr, args.package)
    gh_release = _gh_release_present(versions.github, args.repo)
    gh_tag = _gh_tag_present(versions.github, args.repo)
    result = evaluate(pypi, ghcr, gh_release, gh_tag)

    if args.json:
        print(json.dumps(result))
    elif result["free"]:
        print(f"{args.version} is free: absent from PyPI, ghcr, GitHub releases and Git tags")
    else:
        taken = [
            name
            for name, key in (
                ("PyPI", "pypi_taken"),
                ("ghcr", "ghcr_taken"),
                ("GitHub releases", "github_release_taken"),
                ("Git tags", "github_tag_taken"),
            )
            if result[key] is True
        ]
        if taken:
            print(f"{args.version} is NOT free: already present on {', '.join(taken)}")
        unreachable = cast("list[str]", result["unreachable"])
        if unreachable:
            print(
                f"{args.version} is NOT free: unreachable registries "
                f"{', '.join(unreachable)} (fail-closed)"
            )

    return 0 if result["free"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
