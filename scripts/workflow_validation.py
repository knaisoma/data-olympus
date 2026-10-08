#!/usr/bin/env python3
"""Validate publication inputs before privileged workflow operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = "ghcr.io/knaisoma/data-olympus"
NUMBER = r"(?:0|[1-9][0-9]*)"
STABLE = rf"v{NUMBER}\.{NUMBER}\.{NUMBER}"
RELEASE = rf"{STABLE}(?:(?:-rc\.|-hotfix\.rc\.){NUMBER})?"
CHANNELS = {"rc", "stable", "latest"}


def run(*args: str) -> str:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end="")
        raise


def oci_tag(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("expected one OCI tag, without whitespace or control characters")
    return value


def manual_image(tag: str) -> None:
    if os.environ.get("GITHUB_REF") not in {
        "refs/heads/main", "refs/heads/release/new", "refs/heads/hotfix/new",
    }:
        raise ValueError("manual image source must be an approved branch")
    oci_tag(tag)
    if tag in CHANNELS:
        raise ValueError("manual image tag cannot be a reserved channel")
    if re.fullmatch(rf"{NUMBER}\.{NUMBER}\.{NUMBER}-(?:hotfix\.)?rc\.{NUMBER}",
                    tag.removeprefix("v")):
        raise ValueError("manual image cannot claim a candidate identity")
    if not re.fullmatch(STABLE.removeprefix("v"), tag.removeprefix("v")):
        return
    for alias in ("v" + tag.removeprefix("v"), tag.removeprefix("v")):
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{alias}"],
            check=False,
            capture_output=True,
        )
        if exists.returncode == 0:
            raise ValueError("refusing existing stable version tag")
        if exists.returncode != 1:
            raise ValueError("could not check stable version tag")
        # Existence checks only improve diagnostics. Stable identities remain
        # reserved for promotion even when neither alias exists anywhere.
        image = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", f"{REPO}:{alias}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if image.returncode == 0:
            raise ValueError("refusing existing stable image tag")
        if not re.search(r"manifest unknown|not found|no such manifest", image.stderr, re.I):
            raise ValueError("could not check stable image tag")
    raise ValueError("manual image cannot claim a stable identity")


def manual_python(ref: str) -> tuple[str, bool]:
    if not ref or any(ord(char) < 33 or ord(char) == 127 for char in ref):
        raise ValueError("invalid source ref")
    upload = bool(re.fullmatch(RELEASE, ref))
    if ref.startswith("v") and not upload:
        tag = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{ref}"],
            check=False, capture_output=True,
        )
        if tag.returncode == 0 or re.match(r"v[0-9]", ref):
            raise ValueError("invalid release tag grammar")
        if tag.returncode != 1:
            raise ValueError("could not check source tag")
    try:
        if upload:
            run("git", "show-ref", "--verify", "--", f"refs/tags/{ref}")
            sha = run("git", "rev-parse", "--verify", "--end-of-options",
                      f"refs/tags/{ref}^{{commit}}")
        else:
            # checkout fetches other branches under origin, without local heads.
            # Preserve build-only dispatches, preferring the fetched branch over
            # ambiguous local names, then an immutable full SHA, then local refs.
            refs = [f"refs/remotes/origin/{ref}"]
            if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
                refs.append(ref)
            if ref not in refs:
                refs.append(ref)
            for source in refs:
                resolved = subprocess.run(
                    ["git", "rev-parse", "--verify", "--end-of-options", f"{source}^{{commit}}"],
                    check=False, capture_output=True, text=True,
                )
                if resolved.returncode == 0:
                    sha = resolved.stdout.strip()
                    break
            else:
                raise ValueError("source ref does not resolve to an existing commit")
    except subprocess.CalledProcessError as exc:
        raise ValueError("source ref does not resolve to an existing commit") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid source commit SHA")
    if upload:
        version = ref.removeprefix("v").replace("-hotfix.rc.", ".dev").replace("-rc.", "rc")
        try:
            metadata = tomllib.loads(run("git", "show", f"{sha}:pyproject.toml"))
            declared_version = metadata["project"]["version"]
        except (subprocess.CalledProcessError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise ValueError("cannot read source package metadata version") from exc
        if declared_version != version:
            raise ValueError("source package metadata version does not match release tag")
    return sha, upload


def python_hashes(version: str, dist: Path) -> None:
    """Permit missing files and same-byte retries; refuse conflicting PyPI bytes."""
    artifacts = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if not artifacts:
        raise ValueError("No publishable Python artifacts found")
    local = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}
    url = f"https://pypi.org/pypi/data-olympus/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.load(response)
        remote = {item["filename"]: item["digests"]["sha256"] for item in payload["urls"]}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise ValueError("could not check Python release identity") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("could not check Python release identity") from exc
    if any(local.get(name) != digest for name, digest in remote.items()):
        raise ValueError("PyPI SHA256 mismatch: existing files differ from the build")


def image_outputs(tag: str, channel: str, move_latest: bool) -> str:
    oci_tag(tag)
    if channel and channel not in CHANNELS:
        raise ValueError("channel must be rc, stable, or latest")
    tags = [tag]
    if move_latest:
        tags.append("latest")
    if channel:
        tags.append(channel)
    delimiter = "tags_" + uuid.uuid4().hex
    return (
        f"tags<<{delimiter}\n" + "\n".join(f"{REPO}:{item}" for item in tags) + f"\n{delimiter}\n"
    )


def set_channel(channel: str, source: str) -> None:
    if channel not in CHANNELS:
        raise ValueError("channel must be rc, stable, or latest")
    oci_tag(source)

    def digest(reference: str) -> str:
        result = run(
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{.Manifest.Digest}}",
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
            raise ValueError("invalid image digest")
        return result

    resolved = digest(f"{REPO}:{source}")
    run(
        "docker",
        "buildx",
        "imagetools",
        "create",
        "--tag",
        f"{REPO}:{channel}",
        f"{REPO}@{resolved}",
    )
    if digest(f"{REPO}:{channel}") != resolved:
        raise ValueError("channel digest mismatch")
    print(f"channel {REPO}:{channel} now points at {REPO}@{resolved}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=(
            "manual-image", "manual-python", "python-hashes", "image-tags", "set-channel",
        )
    )
    args = parser.parse_args()
    output = ""
    if args.operation == "manual-image":
        manual_image(os.environ["INPUT_TAG"])
    elif args.operation == "manual-python":
        sha, upload = manual_python(os.environ["REF"])
        output = f"sha={sha}\nupload={str(upload).lower()}\n"
    elif args.operation == "python-hashes":
        python_hashes(os.environ["PYPI_VERSION"], Path("dist"))
    elif args.operation == "image-tags":
        output = image_outputs(
            os.environ["INPUT_TAG"],
            os.environ.get("INPUT_CHANNEL", ""),
            os.environ.get("MOVE_LATEST") == "true",
        )
    else:
        set_channel(os.environ["CHANNEL"], os.environ["SOURCE"])
    if output:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(output)


if __name__ == "__main__":
    main()
