#!/usr/bin/env python3
"""Release deployed-digest gate: resolve the image digest currently deployed
to a named release channel (e.g. "stable", "latest"), fail-closed.

Used as an evidence adapter feeding release_readiness.py's rc_digest_deployed
condition: the evaluator compares this digest against the expected_rc_digest
recorded in the release manifest. A channel with no matching tag, more than
one matching tag (ambiguous), or an unreachable registry does NOT resolve a
digest (fail-closed), never silently skipped.

CLI: `python3 scripts/deployed_digest.py --target <channel>
      [--package data-olympus] [--org knaisoma] [--json]`
Exit 0 = digest resolved, 1 = not resolved, 2 = registry unreachable.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def _tags(version: dict[str, Any]) -> list[str]:
    metadata = version.get("metadata")
    container = metadata.get("container") if isinstance(metadata, dict) else None
    tags = container.get("tags") if isinstance(container, dict) else None
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, str)]


def evaluate(versions: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """PURE. Resolve the digest for the package version tagged `target`.

    Fails closed (digest: None) unless exactly one version carries the
    target tag and that version has a non-empty digest (its "name" field,
    a "sha256:..." string in the GitHub Packages API shape).
    """
    matches = [v for v in versions if target in _tags(v)]
    matched_versions = len(matches)

    digest: str | None = None
    if matched_versions == 1:
        candidate = matches[0].get("name")
        if isinstance(candidate, str) and candidate.strip():
            digest = candidate

    return {
        "target": target,
        "digest": digest,
        "source": f"ghcr:{target}" if digest else None,
        "matched_versions": matched_versions,
    }


def _fetch_versions(package: str, org: str) -> list[dict[str, Any]]:
    """Fetch every package version (one JSON object per line) via gh api.

    This is the digest source: the single seam release_readiness callers
    (and tests) mock to avoid a real network/registry dependency.
    """
    path = f"/orgs/{org}/packages/container/{package}/versions"
    out = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", ".[]"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed:\n{out.stderr}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def _unresolved(target: str, matched_versions: int = 0) -> dict[str, Any]:
    return {
        "target": target,
        "digest": None,
        "source": None,
        "matched_versions": matched_versions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deployed_digest")
    parser.add_argument("--target", required=True, help="release channel/tag, e.g. stable")
    parser.add_argument("--package", default="data-olympus")
    parser.add_argument("--org", default="knaisoma")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        versions = _fetch_versions(args.package, args.org)
    except RuntimeError as exc:
        result = _unresolved(args.target)
        if args.as_json:
            print(json.dumps(result))
        else:
            print(f"deployed digest for {args.target}: UNRESOLVED (registry unreachable)")
        print(str(exc), file=sys.stderr)
        return 2

    result = evaluate(versions, args.target)

    if args.as_json:
        print(json.dumps(result))
    elif result["digest"]:
        print(f"deployed digest for {args.target}: {result['digest']} (source={result['source']})")
    else:
        print(
            f"deployed digest for {args.target}: UNRESOLVED "
            f"(matched {result['matched_versions']} versions, expected exactly 1)"
        )

    return 0 if result["digest"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
