#!/usr/bin/env python3
"""Weekly maintenance inventory: find upgrade steps that need an assessment.

The inventory reads the repository and public metadata only. It never changes
code, branches or dependencies. For each upgrade step that needs a human
assessment it opens one issue carrying the assessment fields of the
maintenance policy, and never opens a second issue for the same application,
dependency and step, whether the first one is open or closed.

Sources:

- Python lifecycle: the feature line in use (the Dockerfile base image tag)
  against https://endoflife.date/api/python.json.
- Base image: the pinned `FROM` digest against the registry's current digest
  for the same tag. A digest refresh is a patch: Dependabot proposes it, so it
  is reported in the summary only and never opens an assessment.
- Direct dependencies in `uv.lock`: the next major release on PyPI.
  Calendar-versioned packages are skipped.
- GitHub Actions: each action pinned by SHA with a `# vN` comment against its
  next major release.

Planning (`plan_issues`) is pure and unit tested; network access is confined
to the fetch helpers. `--dry-run` prints the plan and writes nothing.
Exit status is 1 when any source could not be read, after everything that
could be read was processed, so a partial inventory is never silent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

APPLICATION = "data-olympus"
KEY_MARKER = "maintenance-key"
# Author of issues created with the workflow GITHUB_TOKEN.
BOT_LOGIN = "github-actions[bot]"
_KEY_RE = re.compile(r"<!-- maintenance-key: ([A-Za-z0-9._/:+-]+) -->")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RELEASE_RE = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_ACTION_RE = re.compile(
    r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[^@\s]*)?@([0-9a-f]{40})\s+#\s*v(\d+)\b"
)
_FROM_RE = re.compile(r"^FROM\s+([a-z0-9./-]+):([A-Za-z0-9._-]+)@(sha256:[0-9a-f]{64})", re.M)
USER_AGENT = "data-olympus-maintenance-inventory"

# Maintenance policy waits (days) and adoption windows (days after eligibility).
MAJOR_WAIT = 60
MINOR_WAIT = 14
MAJOR_ADOPTION = 90
SUPPORT_MARGIN = 30


@dataclass(frozen=True)
class Finding:
    """One upgrade step that needs an assessment issue."""

    ecosystem: str  # python, pypi, github-actions
    name: str
    current: str
    target: str
    step: str  # e.g. "3.13-to-3.14" or "2-to-3"
    released: dt.date | None
    references: tuple[str, ...]
    end_of_support: dt.date | None = None
    prerequisites: str = ""

    @property
    def key(self) -> str:
        return f"{APPLICATION}/{self.ecosystem}/{self.name.lower()}/{self.step}"

    @property
    def title(self) -> str:
        step = self.step.replace("-to-", " to ")
        return f"maintenance: assess {self.name} {step} ({self.ecosystem})"


@dataclass
class Inventory:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- planning


def existing_keys(issues: Iterable[dict[str, Any]]) -> set[str]:
    """Keys recorded in issues this workflow opened, open or closed.

    Only issues authored by the workflow's own identity count. On a public
    repository anyone can open an issue, so a marker copied into an issue by
    another author must not suppress a real assessment. Pull requests are
    ignored: the issues API lists them too.
    """
    keys: set[str] = set()
    for issue in issues:
        if issue.get("pull_request"):
            continue
        if (issue.get("user") or {}).get("login") != BOT_LOGIN:
            continue
        keys.update(_KEY_RE.findall(issue.get("body") or ""))
    return keys


def plan_issues(findings: Iterable[Finding], issues: Iterable[dict[str, Any]]) -> list[Finding]:
    """Findings that need a new issue: one per key, none already recorded."""
    seen = existing_keys(issues)
    planned: list[Finding] = []
    for finding in findings:
        if finding.key in seen:
            continue
        seen.add(finding.key)
        planned.append(finding)
    return planned


def _date(value: dt.date | None) -> str:
    return value.isoformat() if value else "unknown, to be established by the owner"


def render_body(finding: Finding, today: dt.date) -> str:
    """Issue body with the assessment fields of the maintenance policy."""
    eligible = finding.released + dt.timedelta(days=MAJOR_WAIT) if finding.released else None
    adoption = eligible + dt.timedelta(days=MAJOR_ADOPTION) if eligible else None
    support = (
        finding.end_of_support - dt.timedelta(days=SUPPORT_MARGIN)
        if finding.end_of_support else None
    )
    state = "Eligible (waiting period over)" if eligible and eligible <= today else "Observed"
    refs = "\n".join(f"  - {ref}" for ref in finding.references) or "  - none found"
    lines = [
        f"Opened by the weekly maintenance inventory on {today.isoformat()}.",
        f"Maturity state: **{state}**. Policy: `docs/maintenance.md`.",
        "",
        f"- **Current version:** `{finding.current}`",
        f"- **Target version:** `{finding.target}` (next step only; take majors sequentially)",
        f"- **Official release, migration and support references:**\n{refs}",
        f"- **Maturity eligibility date and evidence:** {_date(eligible)}, "
        f"{MAJOR_WAIT} days after general availability on {_date(finding.released)} "
        "(release metadata of the source above).",
        "- **Breaking changes and compatibility constraints:** to assess from the "
        "references above.",
        "- **Potential effects on configuration, runtime, data and integrations:** to assess.",
        f"- **Prerequisite upgrades:** {finding.prerequisites or 'to assess.'}",
        "- **Test and recovery plans:** to define; the candidate must pass the full CI and "
        "release gates, and the previous stable release is the recovery point.",
        "- **Owner and target release or milestone:** unassigned; set when triaged.",
        f"- **Security, adoption and end-of-support deadlines:** adoption within "
        f"{MAJOR_ADOPTION} days of eligibility, by {_date(adoption)}"
        + (f"; replace the current line by {_date(support)}, {SUPPORT_MARGIN} days before "
           f"its end of support on {_date(finding.end_of_support)}"
           if finding.end_of_support else "")
        + "; any security advisory deadline is shorter and wins.",
        "",
        f"<!-- {KEY_MARKER}: {finding.key} -->",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parsing


def _major(version: str) -> int | None:
    match = _RELEASE_RE.fullmatch(version)
    return int(match.group(1)) if match else None


def is_stable(version: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)*", version))


def direct_dependencies(lock: dict[str, Any]) -> dict[str, str]:
    """Locked versions of the root package's direct dependencies, all extras."""
    packages = {p["name"]: p for p in lock.get("package", [])}
    root = packages.get(APPLICATION, {})
    names = {d["name"] for d in root.get("dependencies", [])}
    for group in (root.get("optional-dependencies") or {}).values():
        names.update(d["name"] for d in group)
    for group in (root.get("dev-dependencies") or {}).values():
        names.update(d["name"] for d in group)
    return {n: packages[n]["version"] for n in sorted(names) if n in packages}


def pinned_actions(workflow_texts: Iterable[str]) -> dict[str, int]:
    """Actions pinned by SHA with a `# vN` comment, by repository, lowest major."""
    found: dict[str, int] = {}
    for text in workflow_texts:
        for repo, _sha, major in _ACTION_RE.findall(text):
            found[repo] = min(int(major), found.get(repo, int(major)))
    return found


def base_images(dockerfile: str) -> list[tuple[str, str, str]]:
    return _FROM_RE.findall(dockerfile)


def python_line(tag: str) -> str | None:
    match = re.match(r"(3\.\d+)", tag)
    return match.group(1) if match else None


def next_major_release(
    releases: dict[str, dt.date], current_major: int
) -> tuple[str, dt.date] | None:
    """Latest stable release of the next major and that major's first GA date."""
    candidates = {v: d for v, d in releases.items()
                  if is_stable(v.lstrip("v")) and _major(v) == current_major + 1}
    if not candidates:
        return None
    def order(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.lstrip("v").split("."))
    latest = max(candidates, key=order)
    return latest, min(candidates.values())


# ---------------------------------------------------------------- fetching


def _get(url: str, token: str | None = None, method: str = "GET",
         headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, method=method)
    request.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        request.add_header(k, v)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed https URLs)
        return response.read(), dict(response.headers)


def _json(url: str, token: str | None = None) -> Any:
    return json.loads(_get(url, token, headers={"Accept": "application/json"})[0])


def pypi_releases(name: str) -> dict[str, dt.date]:
    data = _json(f"https://pypi.org/pypi/{name}/json")
    out: dict[str, dt.date] = {}
    for version, files in data.get("releases", {}).items():
        live = [f for f in files if not f.get("yanked")]
        if live:
            out[version] = min(dt.date.fromisoformat(f["upload_time"][:10]) for f in live)
    return out


def action_releases(repo: str, token: str | None) -> dict[str, dt.date]:
    data = _json(f"https://api.github.com/repos/{repo}/releases?per_page=100", token)
    return {
        r["tag_name"]: dt.date.fromisoformat(r["published_at"][:10])
        for r in data
        if not r.get("prerelease") and not r.get("draft") and r.get("published_at")
    }


def registry_digest(image: str, tag: str) -> str:
    repo = image if "/" in image else f"library/{image}"
    token = json.loads(_get(
        "https://auth.docker.io/token?service=registry.docker.io"
        f"&scope=repository:{repo}:pull")[0])["token"]
    accept = ", ".join((
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ))
    _, headers = _get(f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}", token,
                      method="HEAD", headers={"Accept": accept})
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered["docker-content-digest"]


# ---------------------------------------------------------------- inventory


def _line_release(value: object, line: str) -> str:
    """The reported latest release of a Python line, or the line itself."""
    text = str(value)
    return text if re.fullmatch(rf"{re.escape(line)}\.\d+", text) else line


def _safe(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name))


def inventory(root: Path, token: str | None) -> Inventory:
    inv = Inventory()
    dockerfile = (root / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    images = base_images(dockerfile)

    # Python lifecycle and base image.
    lines_in_use = {line for _img, tag, _d in images if (line := python_line(tag))}
    try:
        cycles = _json("https://endoflife.date/api/python.json")
        by_cycle = {c["cycle"]: c for c in cycles}
        for line in sorted(lines_in_use):
            current = by_cycle.get(line, {})
            eol = current.get("eol")
            eol_date = dt.date.fromisoformat(eol) if isinstance(eol, str) else None
            nxt = f"3.{int(line.split('.')[1]) + 1}"
            cycle = by_cycle.get(nxt)
            if cycle and cycle.get("releaseDate"):
                inv.findings.append(Finding(
                    ecosystem="python", name="Python", current=line,
                    target=_line_release(cycle.get("latest"), nxt), step=f"{line}-to-{nxt}",
                    released=dt.date.fromisoformat(cycle["releaseDate"]),
                    references=(
                        f"https://docs.python.org/{nxt}/whatsnew/{nxt}.html",
                        "https://devguide.python.org/versions/",
                        "https://endoflife.date/python",
                    ),
                    end_of_support=eol_date,
                    prerequisites="base image tag in deploy/docker/Dockerfile, "
                    "`requires-python` and classifiers in pyproject.toml, CI Python "
                    "versions, and every direct dependency supporting the new line.",
                ))
            inv.notes.append(f"Python {line}: end of support {eol or 'unknown'}.")
    except (urllib.error.URLError, KeyError, ValueError, TypeError) as exc:
        inv.errors.append(f"python lifecycle: {exc}")

    for image, tag, pinned in images:
        try:
            current = registry_digest(image, tag)
            state = "current" if current == pinned else f"refresh available ({current})"
            inv.notes.append(f"Base image {image}:{tag}: pinned {pinned}, {state}.")
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            inv.errors.append(f"base image {image}:{tag}: {exc}")

    # Direct dependencies in uv.lock.
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    for name, version in direct_dependencies(lock).items():
        major = _major(version)
        if major is None or major >= 1900 or not _safe(name):
            continue  # unparseable or calendar versioned
        try:
            found = next_major_release(pypi_releases(name), major)
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            inv.errors.append(f"pypi {name}: {exc}")
            continue
        if found:
            target, released = found
            inv.findings.append(Finding(
                ecosystem="pypi", name=name, current=version, target=target,
                step=f"{major}-to-{major + 1}", released=released,
                references=(f"https://pypi.org/project/{name}/{target}/",),
            ))

    # GitHub Actions pinned by SHA.
    texts = [p.read_text(encoding="utf-8")
             for p in sorted((root / ".github" / "workflows").glob("*.y*ml"))]
    for repo, major in pinned_actions(texts).items():
        try:
            found = next_major_release(action_releases(repo, token), major)
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            inv.errors.append(f"action {repo}: {exc}")
            continue
        if found:
            target, released = found
            inv.findings.append(Finding(
                ecosystem="github-actions", name=repo, current=f"v{major}", target=target,
                step=f"{major}-to-{major + 1}", released=released,
                references=(f"https://github.com/{repo}/releases/tag/{target}",),
            ))
    return inv


def list_issues(repo: str, token: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _json(f"https://api.github.com/repos/{repo}/issues?state=all"
                      f"&per_page=100&page={page}", token)
        if not batch:
            return issues
        issues.extend(batch)
        page += 1


def create_issue(repo: str, token: str, title: str, body: str) -> int:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues", method="POST",
        data=json.dumps({"title": title, "body": body}).encode(),
    )
    for k, v in {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}"}.items():
        request.add_header(k, v)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return int(json.loads(response.read())["number"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; open no issues")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or None
    today = dt.datetime.now(dt.UTC).date()

    inv = inventory(args.root, token)
    issues: list[dict[str, Any]] = []
    if args.repo and token:
        try:
            issues = list_issues(args.repo, token)
        except urllib.error.URLError as exc:
            inv.errors.append(f"issue listing: {exc}")
            if not args.dry_run:
                print("::error::cannot list issues; refusing to open any (dedup unknown)")
                return 1
    elif not args.dry_run:
        print("::error::--repo and GITHUB_TOKEN are required unless --dry-run")
        return 2

    planned = plan_issues(inv.findings, issues)
    print(f"## Maintenance inventory {today.isoformat()}\n")
    for note in inv.notes:
        print(f"- {note}")
    print(f"\nFindings: {len(inv.findings)}; new assessment issues: {len(planned)}"
          + (" (dry run, nothing opened)" if args.dry_run else ""))
    for finding in planned:
        if args.dry_run:
            print(f"\n### {finding.title}\n\n{render_body(finding, today)}")
        else:
            number = create_issue(args.repo, token or "", finding.title,
                                  render_body(finding, today))
            print(f"- opened #{number}: {finding.title}")
    for error in inv.errors:
        print(f"::error::{error}")
    return 1 if inv.errors else 0


if __name__ == "__main__":
    sys.exit(main())
