#!/usr/bin/env python3
"""Release deployed-digest gate: resolve the image digest currently deployed
to a named release channel (e.g. "stable", "latest"), fail-closed.

Used as an evidence adapter feeding release_readiness.py's rc_digest_deployed
condition: the evaluator compares this digest against the expected_rc_digest
recorded in the release manifest. The exact public tag must return one top
level OCI index digest. A missing tag, ambiguous output, unavailable client,
or unreachable registry does not resolve a digest and fails closed.

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


def _inspect_digest(target: str, package: str, org: str) -> str:
    """Resolve one public tag without requiring GitHub Packages API scope."""
    reference = f"ghcr.io/{org}/{package}:{target}"
    try:
        out = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", reference],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ghcr inspection timed out for {reference}") from exc
    except OSError as exc:
        raise RuntimeError(f"ghcr inspection could not start for {reference}") from exc
    if out.returncode != 0:
        raise RuntimeError(f"ghcr inspection failed for {reference}")
    digests = re.findall(
        r"^Digest:\s*(sha256:[0-9a-f]{64})\s*$",
        out.stdout,
        re.MULTILINE,
    )
    if len(digests) != 1:
        raise RuntimeError(
            f"ghcr inspection returned an ambiguous digest for {reference}"
        )
    return digests[0]


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
        digest = _inspect_digest(args.target, args.package, args.org)
        if type(digest) is not str or _DIGEST_RE.fullmatch(digest) is None:
            raise RuntimeError("ghcr inspection returned an invalid digest")
        result = {
            "target": args.target,
            "digest": digest,
            "source": f"ghcr:{args.target}",
            "matched_versions": 1,
        }
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
