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
import re
import subprocess
import sys
from typing import Any

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _tags(version: Any) -> list[str]:
    if not isinstance(version, dict):
        return []
    metadata = version.get("metadata")
    container = metadata.get("container") if isinstance(metadata, dict) else None
    tags = container.get("tags") if isinstance(container, dict) else None
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, str)]


def evaluate(versions: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """PURE. Resolve the digest for the package version tagged `target`.

    Fails closed (digest: None) unless exactly one version carries the
    target tag and that version has a digest (its "name" field) matching
    the expected "sha256:<64 hex chars>" shape. Any other shape (empty,
    truncated, non-hex, or not a string at all) is treated as no valid
    digest, same as a no-match or ambiguous-match result.
    """
    matches = [v for v in versions if target in _tags(v)]
    matched_versions = len(matches)

    digest: str | None = None
    if matched_versions == 1:
        candidate = matches[0].get("name")
        if isinstance(candidate, str) and _DIGEST_RE.match(candidate):
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
    (and tests) mock to avoid a real network/registry dependency. Every
    failure mode (missing gh binary, subprocess failure, malformed JSON) is
    normalized to RuntimeError so callers have one exception type to expect;
    main() still fails closed on any other exception type as a backstop.
    """
    path = f"/orgs/{org}/packages/container/{package}/versions"
    try:
        out = subprocess.run(
            ["gh", "api", "--paginate", path, "--jq", ".[]"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        # e.g. the "gh" binary is not installed or not on PATH.
        raise RuntimeError(f"gh api {path} could not be started: {exc}") from exc
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed:\n{out.stderr}")
    try:
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api {path} returned malformed JSON: {exc}") from exc


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
        if not isinstance(versions, list):
            raise TypeError(f"expected a list of package versions, got {type(versions).__name__}")
        result = evaluate(versions, args.target)
    except Exception as exc:
        # Fail-closed backstop: any failure resolving or evaluating the
        # digest lookup (subprocess/gh missing, malformed JSON, an
        # unexpectedly-shaped payload, etc.) must emit the clean
        # {"digest": null, "source": null} contract and a non-zero exit,
        # never an uncaught traceback.
        result = _unresolved(args.target)
        if args.as_json:
            print(json.dumps(result))
        else:
            print(f"deployed digest for {args.target}: UNRESOLVED (lookup failed)")
        print(str(exc), file=sys.stderr)
        return 2

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
