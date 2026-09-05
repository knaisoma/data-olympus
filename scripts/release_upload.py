"""Complete a GitHub release upload without replacing published bytes."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def upload(tag: str, files: list[Path]) -> None:
    """Validate every collision before uploading only missing assets.

    Upload deliberately omits replacement flags, so a concurrent upload of a
    missing asset fails instead of overwriting it. Rerun to verify its bytes.
    Inventory and download failures propagate without any upload.
    """
    result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "assets"],
        check=True, capture_output=True,
    )
    names = {asset["name"] for asset in json.loads(result.stdout)["assets"]}
    missing = []
    for path in files:
        local = path.read_bytes()
        if path.name not in names:
            missing.append(path)
            continue
        remote = subprocess.run(
            ["gh", "release", "download", tag, "--pattern", path.name, "--output", "-"],
            check=True, capture_output=True,
        ).stdout
        if local != remote:
            raise ValueError(f"Published asset {path.name} has different bytes; use a new version")
    if missing:
        subprocess.run(
            ["gh", "release", "upload", tag, *(str(path) for path in missing)], check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args()
    upload(args.tag, args.files)
