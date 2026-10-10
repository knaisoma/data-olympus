#!/usr/bin/env python3
"""Compute STD-U-821 candidates without changing the old release path.

Callers must supply full history and tags. The CLI can fetch them explicitly
with --fetch; computation itself is read-only. JSON preserves the reference
engine's provenance fields and adds pypi_version. --format env emits the W6
build contract. GA approval is read only from H, adoption only from untagged B.

Adoption records the sole parent of untagged B as anchor, where the record
must be absent, and the base version X.Y.Z, whose vX.Y.Z must be the highest
stable tag, an ancestor of B and a strict ancestor of the anchor (STD-U-821
amendment 1.5). Every v<digit> tag in the repository must be a strict stable
or prerelease version, so a malformed tag cannot hide a higher release.
Adoption parses impact from the stable base, but counts from B.
The cut must add only release/ADOPTION.json, and promotion requires its absence
from H. Adoption JSON includes adoption_retired to report that absence.
It requires two ratifications, each YYYY-MM-DD:<ref> with a nonempty reference
token and a date no later than today UTC, and each with a standard file
carrying the exact line Ratification: YYYY-MM-DD inside its amendment section:
--adoption-ratified with --standard-file (amendment 1.3) and
--extension-ratified with --extension-standard-file (amendment 1.5); the API
names are adoption_ratified, standard_file, extension_ratified and
extension_standard_file. --adoption-dry-run evaluates a record that lacks
them without allowing promotion; a ratification that is given is always
checked.

ignore_tags (API only, default empty) removes exact stable tag names from the
enumeration of released versions, which decides the highest stable tag for the
adoption record and the hotfix scope and the released-product check. Promotion
passes only its own stable tag, after verifying it, because the tag it creates
on S would otherwise make every later recomputation of the same release see a
higher stable version than its base. A name that is not a strict vX.Y.Z or
that tags the cut itself is refused.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.compute_release import classify  # noqa: E402

_NUMBER = r"(0|[1-9][0-9]*)"
_STABLE = re.compile(rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}")
_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_PRERELEASE = re.compile(
    rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}-{_IDENTIFIER}(?:\.{_IDENTIFIER})*"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_SUBJECT = re.compile(r"[a-z]+(?:\([a-z0-9._/-]+\))?!?: \S.*")
_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: \S", re.MULTILINE)


class VersionError(Exception):
    """Domain errors and exit statuses shared with version.mjs."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.exit_code = {"recut_required": 3, "bad_bootstrap_cut": 4, "git_error": 5}.get(
            code, 1,
        )
        super().__init__(f"{code}: {message}")


class Git:
    def __init__(self, cwd: str | Path):
        self.cwd = cwd

    def run(self, *args: str, allow_one: bool = False) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.cwd, capture_output=True, text=True, check=False,
            )
        except OSError as error:
            raise VersionError("git_error", str(error)) from error
        if result.returncode and not (allow_one and result.returncode == 1):
            raise VersionError("git_error", result.stderr.strip())
        return result.stdout

    def resolve(self, ref: str) -> str:
        try:
            return self.run(
                "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
            ).strip()
        except VersionError as error:
            raise VersionError("bad_ref", "head and main must resolve to commits") from error

    def ancestor(self, ancestor: str, head: str) -> bool:
        # rev-list avoids treating Git errors as negative ancestry results.
        return not self.run("rev-list", "--max-count=1", f"{head}..{ancestor}").strip()

    def file(self, commit: str, path: str) -> str | None:
        if not self.run("ls-tree", commit, "--", path).strip():
            return None
        return self.run("show", f"{commit}:{path}")


def _version(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag[1:].split("."))


def _impact(message: str) -> int:
    subject, _, body = message.partition("\n")
    if not _SUBJECT.fullmatch(subject):
        raise VersionError("malformed_commit", f"malformed commit: {subject[:80]}")
    # Reuse the legacy parser, filtering its permissive footer grammar first.
    # Empty markers and incidental prose must never produce a breaking bump.
    footers = "\n".join(line for line in body.splitlines() if _FOOTER.match(line))
    kind, breaking = classify(subject, footers)
    return 3 if breaking else 2 if kind == "feat" else 1 if kind == "fix" else 0


def _adoption_base(git: Git, cut: str, tags: list[str], every_tag: list[str]) -> str | None:
    raw = git.file(cut, "release/ADOPTION.json")
    if raw is None:
        return None
    # Amendment 1.5: a malformed version tag anywhere could hide a higher
    # release from the highest-tag rule, so refuse the record outright.
    malformed = [tag for tag in every_tag if re.match(r"v[0-9]", tag)
                 and not _STABLE.fullmatch(tag) and not _PRERELEASE.fullmatch(tag)]
    if malformed:
        raise VersionError(
            "invalid_stable_tag", f"non-strict version tags in repository: {', '.join(malformed)}",
        )
    try:
        record = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise VersionError("bad_adoption", "adoption record must be a JSON object") from error
    # The base comes from the record, so it must be a strict X.Y.Z whose tag
    # exists, is the highest stable tag by precedence, and is an ancestor of B.
    base = record.get("base") if isinstance(record, dict) else None
    tag = f"v{base}" if isinstance(base, str) and _STABLE.fullmatch(f"v{base}") else None
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("anchor"), str)
        or not re.fullmatch(rf"[0-9a-fA-F]{{{len(cut)}}}", record["anchor"])
        or tag is None
        or tag not in tags
        or max(tags, key=_version) != tag
        or not git.ancestor(git.resolve(f"refs/tags/{tag}"), cut)
    ):
        raise VersionError("bad_adoption", "record must name untagged cut and highest stable base")
    try:
        anchor = git.resolve(record["anchor"])
    except VersionError as error:
        raise VersionError("bad_adoption", "anchor must resolve to a commit") from error
    if (
        git.run("rev-list", "--parents", "-n", "1", cut).split() != [cut, anchor]
        or git.file(anchor, "release/ADOPTION.json") is not None
    ):
        raise VersionError("bad_adoption", "anchor must be sole parent and have no adoption record")
    # Amendment 1.5: the route exists only while main is ahead of the tag.
    if int(git.run("rev-list", "--count", f"{git.resolve(f'refs/tags/{tag}')}..{anchor}")) < 1:
        raise VersionError("bad_adoption", "base tag must be a strict ancestor of the anchor")
    if git.run("diff", "--no-ext-diff", "--name-status", anchor, cut).splitlines() != [
        "A\trelease/ADOPTION.json",
    ]:
        raise VersionError("bad_adoption", "cut must add only release/ADOPTION.json")
    return tag


_AMENDMENTS = (("1.3", "adoption_ratified", "standard_file"),
               ("1.5", "extension_ratified", "extension_standard_file"))


def _check_ratification(
    amendment: str, ratified: object, standard_file: object, today: date,
) -> None:
    """Require <date>:<ref> and the exact Ratification line in the amendment's section."""
    try:
        if not isinstance(ratified, str) or not isinstance(standard_file, (str, Path)):
            raise ValueError
        match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2}):\S+", ratified)
        if match is None:
            raise ValueError
        ratified_date = match[1]
        if date.fromisoformat(ratified_date) > today:
            raise ValueError
        # The engine, not each caller, requires the standard's ratification line.
        lines = Path(standard_file).read_text(encoding="utf-8").splitlines()
        number = re.escape(amendment)
        heading = next((i for i, line in enumerate(lines) if re.fullmatch(
            rf"## (?:Amendment|Proposed amendment) {number}(?:: .+)?", line,
        )), None)
        if heading is None:
            raise ValueError
        section_start = heading + 1
        section_end = next((i for i in range(section_start, len(lines))
                            if lines[i].startswith(("### ", "## "))), len(lines))
        if f"Ratification: {ratified_date}" not in lines[section_start:section_end]:
            raise ValueError
    except (ValueError, OSError) as error:
        raise VersionError(
            "adoption_unratified",
            f"amendment {amendment} ratification requires YYYY-MM-DD:<ref>, a non-future "
            f"UTC date, and a matching Ratification line under ## Amendment {amendment} "
            f"or ## Proposed amendment {amendment} before the next section in its "
            "standard file",
        ) from error


def compute_version(
    *, cwd: str | Path, head: str, main: str, branch: str,
    bootstrap: dict[str, str] | None = None,
    adoption_ratified: str | None = None, adoption_dry_run: bool = False,
    standard_file: str | Path | None = None, today: date | None = None,
    ignore_tags: frozenset[str] = frozenset(),
    extension_ratified: str | None = None,
    extension_standard_file: str | Path | None = None,
) -> dict:
    """Freeze refs, validate the cut, and derive a candidate from its commit set.

    bootstrap is the adoption issue's immutable {cut, target} record for a
    never-released product. There is intentionally no GA argument or env override.
    """
    if branch not in ("release/new", "hotfix/new"):
        raise VersionError("bad_branch", "expected release/new or hotfix/new")
    # Exact names only: a set or list of patterns could hide any tag.
    if not isinstance(ignore_tags, frozenset) or not all(
            isinstance(tag, str) and _STABLE.fullmatch(tag) for tag in ignore_tags):
        raise VersionError("bad_ignore_tags", "ignore_tags must be exact vX.Y.Z names")
    git = Git(cwd)
    if git.run("rev-parse", "--is-shallow-repository").strip() == "true":
        raise VersionError("git_error", "full history and tags required; fetch before computing")
    if bootstrap is not None and "cut" in bootstrap:
        length = 64 if git.run("rev-parse", "--show-object-format").strip() == "sha256" else 40
        if not isinstance(bootstrap["cut"], str) or not re.fullmatch(
            rf"[0-9a-fA-F]{{{length}}}", bootstrap["cut"],
        ):
            raise VersionError("bad_bootstrap_cut", "bootstrap cut must be a full hexadecimal SHA")
    h, m = git.resolve(head), git.resolve(main)
    bases = git.run("merge-base", "--all", m, h, allow_one=True).splitlines()
    if not bases:
        raise VersionError("no_merge_base", "no common ancestor; check full history and refs")
    if len(bases) != 1:
        raise VersionError("multiple_merge_bases", f"expected one merge base, found {len(bases)}")
    b = bases[0]
    if not git.ancestor(m, h):
        raise VersionError("recut_required", "main advanced; preserve pending work and recut")

    # The standard's exact describe lookup is advisory; enumeration below is
    # authoritative, including when a prerelease or unrelated tag sorts first.
    with contextlib.suppress(VersionError):
        git.run("describe", "--tags", "--exact-match", "--match", "v[0-9]*", b)
    tags = git.run("tag", "--list").splitlines()
    base_tags = git.run("tag", "--points-at", b).splitlines()
    invalid = [tag for tag in base_tags if re.match(r"v[0-9]", tag)
               and not _STABLE.fullmatch(tag) and not _PRERELEASE.fullmatch(tag)]
    if invalid:
        raise VersionError("invalid_stable_tag", f"non-strict version tags: {', '.join(invalid)}")
    stable = [tag for tag in base_tags if _STABLE.fullmatch(tag)]
    if ignore_tags & set(stable):
        raise VersionError("bad_ignore_tags", "an ignored tag cannot tag the cut")
    all_stable = [tag for tag in tags if _STABLE.fullmatch(tag) and tag not in ignore_tags]
    if len(stable) > 1:
        raise VersionError("ambiguous_stable_tag", "more than one stable tag on cut")
    # A stable cut tag takes precedence even if an adoption record remains.
    base = stable[0] if stable else _adoption_base(git, b, all_stable, tags)
    if branch == "hotfix/new" and (not stable or base != max(all_stable, key=_version)):
        raise VersionError("hotfix_scope", "hotfix/new requires the current stable main tag")
    adoption = base is not None and not stable
    ratifications = {"adoption_ratified": adoption_ratified, "standard_file": standard_file,
                     "extension_ratified": extension_ratified,
                     "extension_standard_file": extension_standard_file}
    ratified = False
    if adoption:
        # Amendment 1.5: a valid record in effect needs both ratifications.
        for amendment, value, document in _AMENDMENTS:
            if ratifications[value] is not None:
                _check_ratification(amendment, ratifications[value],
                                    ratifications[document], today or datetime.now(UTC).date())
        missing = [amendment for amendment, value, _ in _AMENDMENTS
                   if ratifications[value] is None]
        ratified = not missing
        if missing and not adoption_dry_run:
            raise VersionError(
                "adoption_unratified",
                f"amendment {' and '.join(missing)} ratification missing: pass "
                "--adoption-ratified and --extension-ratified YYYY-MM-DD:<ref> or dry-run",
            )

    ga = False
    if branch == "release/new":
        decision = git.file(h, "release/GA-DECISION")
        if decision is not None:
            if decision.strip() != "approved":
                raise VersionError("bad_ga_decision", "release/GA-DECISION must contain approved")
            ga = True
    if base is None:
        if all_stable:
            raise VersionError("missing_tags_in_released_product", "no stable tag on cut")
        if (
            not bootstrap or not bootstrap.get("cut")
            or bootstrap.get("target") not in ("0.1.0", "1.0.0")
            or (bootstrap["target"] == "1.0.0" and not ga)
        ):
            raise VersionError("bad_bootstrap", "first release needs a recorded cut and target")
        try:
            recorded = git.resolve(bootstrap["cut"])
        except VersionError:
            recorded = None
        if recorded != b:
            raise VersionError("bootstrap_mismatch", "bootstrap cut does not equal merge base")

    count = int(git.run("rev-list", "--count", f"{b}..{h}"))
    start = git.resolve(f"refs/tags/{base}") if adoption else b
    raw = git.run("log", "--no-merges", "-z", "--format=%B", f"{start}..{h}")
    highest = 0
    for message in raw.split("\0")[:-1]:
        impact = _impact(message)
        if branch == "hotfix/new" and impact > 1:
            raise VersionError("hotfix_scope", "hotfix/new accepts fixes only")
        highest = max(highest, impact)
    if base is None:
        assert bootstrap is not None
        target = bootstrap["target"]
    else:
        major, minor, patch = _version(base)
        if major == 0 and ga:
            target = "1.0.0"
        elif highest == 3:
            target = f"{major + 1}.0.0" if major else f"0.{minor + 1}.0"
        elif highest == 2 and major:
            target = f"{major}.{minor + 1}.0"
        else:
            target = f"{major}.{minor}.{patch + 1}"
    hotfix = branch == "hotfix/new"
    candidate = f"{target}-{'hotfix.rc' if hotfix else 'rc'}.{count}"
    adoption_retired = not git.run("ls-tree", h, "--", "release/ADOPTION.json").strip()
    return {
        "base": base, "B": b, "H": h, "M": m, "N": count, "target": target,
        "candidate": candidate, "pypi_version": f"{target}{'.dev' if hotfix else 'rc'}{count}",
        "promotable": count > 0 and adoption_retired and (not adoption or ratified),
        **({"adoption": "ratified" if ratified else "unratified",
            "adoption_retired": adoption_retired} if adoption else {}),
    }


def env_lines(result: dict) -> str:
    """Serialize validated values as the six W6 build variables."""
    return (
        f"RC_VERSION={result['candidate']}\nPYPI_VERSION={result['pypi_version']}\n"
        f"TARGET={result['target']}\nCUT_SHA={result['B']}\nN={result['N']}\n"
        f"PROMOTABLE={str(result['promotable']).lower()}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", default=os.environ.get("CI_COMMIT_SHA", "HEAD"))
    parser.add_argument("--main", default="origin/main")
    parser.add_argument("--branch", default=os.environ.get("CI_COMMIT_BRANCH"))
    parser.add_argument("--bootstrap-cut", default=os.environ.get("RELEASE_BOOTSTRAP_CUT"))
    parser.add_argument(
        "--bootstrap-target", default=os.environ.get("RELEASE_BOOTSTRAP_TARGET", ""),
    )
    parser.add_argument("--fetch", action="store_true", help="fetch origin main and all tags first")
    parser.add_argument("--format", choices=("json", "env"), default="json")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--env-output", type=Path)
    parser.add_argument("--adoption-ratified", metavar="YYYY-MM-DD:<ref>")
    parser.add_argument("--standard-file", type=Path)
    parser.add_argument("--extension-ratified", metavar="YYYY-MM-DD:<ref>")
    parser.add_argument("--extension-standard-file", type=Path)
    parser.add_argument("--adoption-dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.adoption_ratified is not None and args.standard_file is None:
            raise VersionError(
                "adoption_unratified", "--adoption-ratified requires --standard-file",
            )
        if args.extension_ratified is not None and args.extension_standard_file is None:
            raise VersionError(
                "adoption_unratified", "--extension-ratified requires --extension-standard-file",
            )
        if args.fetch:
            git = Git(Path.cwd())
            shallow = git.run("rev-parse", "--is-shallow-repository").strip() == "true"
            git.run("fetch", "--quiet", "--tags", *(["--unshallow"] if shallow else []),
                    "origin", "+refs/heads/main:refs/remotes/origin/main")
        result = compute_version(
            cwd=Path.cwd(), head=args.head, main=args.main, branch=args.branch,
            bootstrap={"cut": args.bootstrap_cut, "target": args.bootstrap_target}
            if args.bootstrap_cut else None,
            adoption_ratified=args.adoption_ratified, adoption_dry_run=args.adoption_dry_run,
            standard_file=args.standard_file,
            extension_ratified=args.extension_ratified,
            extension_standard_file=args.extension_standard_file,
        )
        document = json.dumps(result, indent=2) + "\n"
        env = env_lines(result)
        if args.json_output:
            args.json_output.write_text(document)
        if args.env_output:
            args.env_output.write_text(env)
        print(document if args.format == "json" else env, end="")
    except VersionError as error:
        print(error, file=sys.stderr)
        return error.exit_code
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
