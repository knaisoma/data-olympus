#!/usr/bin/env python3
"""Validate titles, source commits and squash impact (STD-U-821).

The positional title-only CLI retains its STD-U-810 behavior. Range mode uses
the STD-U-821 grammar and reads PR_TITLE from the environment, with optional
--body for the proposed squash message body. Scopes contain one or more ASCII
lowercase letters, digits, periods, underscores, slashes or hyphens [a-z0-9._/-].
The subject
``release: X.Y.Z`` is accepted only when the caller reports a same-repository
pull request from ``release/new`` or ``hotfix/new`` into ``main``. It needs only
the standard library and Git, so CI can run a trusted base-ref copy without
installing or executing anything from a pull request.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/x.py): put repo root on the
# path so `scripts.*` imports resolve the same way they do under pytest.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.compute_release import classify  # noqa: E402

_ALLOWED = {
    "feat", "fix", "perf", "chore", "docs",
    "refactor", "test", "ci", "build", "style", "revert",
}

_SUBJECT = re.compile(r"([a-z]+)(?:\([a-z0-9._/-]+\))?(!)?: (\S.*)")
# STD-U-821 release squash subject: strict X.Y.Z, no prefix, no suffix.
_RELEASE_SUBJECT = re.compile(r"release: (0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_RELEASE_BASE = "main"
_RELEASE_HEADS = frozenset({"release/new", "hotfix/new"})
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: \S", re.MULTILINE)
_IMPACTS = ("other", "fix", "feat", "breaking")


def commit_impact(message: str) -> int:
    """Return other=0, fix=1, feat=2, breaking=3; reject malformed messages.

    A rewritten revert is an ordinary ``revert: ...`` commit. Its embedded
    original subject contributes no impact, and it never subtracts impact.
    """
    subject, _, body = message.partition("\n")
    match = _SUBJECT.fullmatch(subject)
    if match is None:
        raise ValueError(f"Malformed commit subject: {subject!r}")
    if match[2] or _BREAKING_FOOTER.search(body):
        return 3
    return {"feat": 2, "fix": 1}.get(match[1], 0)


def is_release_squash(base_ref: str, head_ref: str, same_repo: bool) -> bool:
    """True only for a same-repository release/new or hotfix/new PR into main."""
    return same_repo and base_ref == _RELEASE_BASE and head_ref in _RELEASE_HEADS


def lint_range(
    repo: Path, base: str, head: str, title: str, body: str = "",
    *, release_squash: bool = False,
) -> list[str]:
    """Lint every non-merge message and the proposed squash title and body.

    ``release_squash`` is set by the caller only for the release or hotfix
    integration PR into main (see ``is_release_squash``). Only then is the
    subject ``release: X.Y.Z`` accepted.
    """
    # Resolve first, preventing option/revision-expression injection into log.
    refs = [subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
         f"{ref}^{{commit}}"], check=True, capture_output=True, text=True,
    ).stdout.strip() for ref in (base, head)]
    raw = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-merges", "-z", "--format=%H%n%B",
         f"{refs[0]}..{refs[1]}", "--"],
        check=True, capture_output=True, text=True,
    ).stdout
    errors: list[str] = []
    highest = 0
    has_breaking_footer = False
    # Remove only the final record terminator, never message whitespace.
    for record in raw.removesuffix("\0").split("\0") if raw else []:
        sha, _, message = record.partition("\n")
        has_breaking_footer |= bool(_BREAKING_FOOTER.search(message.partition("\n")[2]))
        try:
            highest = max(highest, commit_impact(message))
        except ValueError as exc:
            errors.append(f"{sha}: {exc}")
    try:
        if "\n" in title or "\r" in title:
            raise ValueError("PR title must be a single subject line")
        if release_squash and _RELEASE_SUBJECT.fullmatch(title):
            # The release squash carries no conventional impact of its own:
            # the engine computes the version from the source commits, and
            # promotion proves subject and body byte for byte. Comparing an
            # impact here would be meaningless, so the comparison is skipped.
            proposed = highest
        else:
            if title.startswith("release"):
                subject = _SUBJECT.fullmatch(title)
                if subject is not None and subject[1] == "release":
                    raise ValueError(
                        "type 'release' is reserved for 'release: X.Y.Z' on a "
                        "release/new or hotfix/new pull request into main"
                    )
            proposed = commit_impact(f"{title}\n\n{body}")
        if proposed < highest:
            errors.append(
                f"Proposed squash impact {_IMPACTS[proposed]} is lower than "
                f"source impact {_IMPACTS[highest]}; preserve breaking markers."
            )
    except ValueError as exc:
        errors.append(f"Invalid PR title: {exc}")
    if has_breaking_footer and not _BREAKING_FOOTER.search(body):
        errors.append(
            "Proposed squash body must preserve a nonempty breaking-change footer "
            "(BREAKING CHANGE: or BREAKING-CHANGE:); '!' alone is insufficient."
        )
    return errors


def is_valid_title(title: str) -> bool:
    ctype, _ = classify(title, "")
    return ctype in _ALLOWED


def main(argv: list[str]) -> int:
    if argv and argv[0].startswith("--"):
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--base", required=True)
        parser.add_argument("--head", default="HEAD")
        parser.add_argument("--repo", type=Path, default=Path.cwd())
        parser.add_argument("--body", default="", help="Proposed squash message body")
        parser.add_argument("--base-ref", default="", help="PR base branch name")
        parser.add_argument("--head-ref", default="", help="PR head branch name")
        parser.add_argument("--base-repo", default="", help="PR base repository full name")
        parser.add_argument("--head-repo", default="", help="PR head repository full name")
        args = parser.parse_args(argv)
        try:
            same_repo = bool(args.base_repo) and args.base_repo == args.head_repo
            errors = lint_range(
                args.repo, args.base, args.head, os.environ.get("PR_TITLE", ""), args.body,
                release_squash=is_release_squash(args.base_ref, args.head_ref, same_repo),
            )
        except UnicodeDecodeError as exc:
            print(
                f"Cannot decode commit range: invalid {exc.encoding} output from Git",
                file=sys.stderr,
            )
            return 1
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"Cannot read commit range: {exc}", file=sys.stderr)
            return 1
        for error in errors:
            print(error, file=sys.stderr)
        if not errors:
            print("Commit range and squash title ok")
        return int(bool(errors))
    title = argv[0] if argv else ""
    if is_valid_title(title):
        print(f"PR title ok: {title}")
        return 0
    print(
        f"Invalid PR title: {title!r}\n"
        f"Must be a Conventional Commit: type(scope): subject, "
        f"type in {sorted(_ALLOWED)}; add '!' or a 'BREAKING CHANGE:' footer for breaking.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
