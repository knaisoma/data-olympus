#!/usr/bin/env python3
"""Release version-freshness gate: fail unless a target version is absent from
every external registry (PyPI, ghcr, GitHub releases), fail-closed.

A version is only "free" (safe to publish) when it is confirmed absent from
all three registries AND every registry was reachable. If any registry could
not be queried, the version is treated as NOT free: never assume free during
an outage, since republishing an already-taken (immutable) version is
forbidden.

CLI: `python3 scripts/version_free.py --version X.Y.Z [--package data-olympus]
[--repo knaisoma/data-olympus] [--json]`
Exit 0 = free (safe to publish), 1 = taken or unreachable.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import cast

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+].+)?$")


def evaluate(
    pypi_present: bool | None,
    ghcr_present: bool | None,
    gh_release_present: bool | None,
) -> dict[str, object]:
    """PURE. Each arg is True (found/taken), False (confirmed absent), or
    None (unreachable/unknown). A version is free only if all three are
    exactly False; any True means taken, any None means unreachable and
    fails closed (not free)."""
    checks = (
        ("pypi", pypi_present),
        ("ghcr", ghcr_present),
        ("github_release", gh_release_present),
    )
    unreachable = [name for name, present in checks if present is None]
    free = all(present is False for _, present in checks)
    result: dict[str, object] = {
        "pypi_taken": pypi_present,
        "ghcr_taken": ghcr_present,
        "github_release_taken": gh_release_present,
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
    except urllib.error.URLError:
        return None


def _ghcr_present(version: str, package: str = "data-olympus") -> bool | None:
    tag = f"v{version}"
    out = subprocess.run(
        [
            "gh", "api",
            "--paginate",
            f"/orgs/knaisoma/packages/container/{package}/versions",
            "--jq", ".[].metadata.container.tags[]",
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    tags = {line.strip() for line in out.stdout.splitlines() if line.strip()}
    return tag in tags


def _gh_release_present(version: str, repo: str = "knaisoma/data-olympus") -> bool | None:
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/tags/v{version}"],
        capture_output=True, text=True,
    )
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

    if not _SEMVER_RE.match(args.version):
        print(f"invalid semver: {args.version!r}", file=sys.stderr)
        return 1

    pypi = _pypi_present(args.version, args.package)
    ghcr = _ghcr_present(args.version, args.package)
    gh_release = _gh_release_present(args.version, args.repo)
    result = evaluate(pypi, ghcr, gh_release)

    if args.json:
        print(json.dumps(result))
    elif result["free"]:
        print(f"{args.version} is free: absent from PyPI, ghcr, and GitHub releases")
    else:
        taken = [
            name
            for name, key in (
                ("PyPI", "pypi_taken"),
                ("ghcr", "ghcr_taken"),
                ("GitHub releases", "github_release_taken"),
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
