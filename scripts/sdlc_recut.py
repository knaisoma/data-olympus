#!/usr/bin/env python3
"""Plan verified release recuts and hotfix cuts; apply only under the SDLC lock.

No candidate code is checked out or executed. Publication/delivery evidence is
an explicit operator input, not something this tool invents from a Git tag.
Planning fetches history and writes disposable Git objects for replay checks,
but changes no branch. Pure plan functions consume the verified facts.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import adoption_ratification as ratification  # noqa: E402
from scripts import sdlc_version as engine  # noqa: E402

MAIN = "refs/heads/main"
RELEASE = "refs/heads/release/new"
HOTFIX = "refs/heads/hotfix/new"
PRESERVE = "refs/heads/sdlc-preserve/"
# Old-path privileged workflows outside the data-olympus-promotion lock group.
OLD_PATH_WORKFLOWS = ("tag-release.yml", "rc-publish.yml", "set-channel.yml")


class RecutError(Exception):
    """Unsafe or unreconciled state; no remote mutation is permitted."""


def _sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise RecutError("expected a full lowercase commit SHA")
    return value


def _git(cwd, *args):
    try:
        return engine.Git(cwd).run(*args).strip()
    except engine.VersionError as error:
        raise RecutError(str(error)) from error


def _tag_commit(git, ref):
    """Resolve a tag to its commit, naming the tag when it does not peel to one."""
    try:
        return git.resolve(ref)
    except engine.VersionError as error:
        raise RecutError(f"bad_ref: {ref} does not resolve to a commit") from error


def refuse_old_path_activity(runs):
    """Refuse while an old-path privileged run is queued or in progress.

    The caller passes the workflow runs it listed; this is a point-in-time
    check, not a lock. An old-path run that starts afterwards is not blocked.
    """
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        raise RecutError("old-path runs must be a JSON list of objects")
    for run in runs:
        path = run.get("path")
        if not isinstance(path, str):
            raise RecutError("old-path run without a workflow path; state uncertain")
        if run.get("status") == "completed":
            continue
        if path.split("@", 1)[0].rsplit("/", 1)[-1] in OLD_PATH_WORKFLOWS:
            raise RecutError(f"old-path workflow run active: {path} ({run.get('status')})")


def validate_acknowledge(acknowledge):
    """Acknowledgements are a list of full SHA strings, never another JSON value."""
    if not isinstance(acknowledge, (list, tuple)) or any(
        not isinstance(sha, str) for sha in acknowledge
    ):
        raise RecutError("acknowledge must be a JSON list of SHA strings")
    for sha in acknowledge:
        _sha(sha)
    return list(acknowledge)


def remote_snapshot(cwd, remote):
    """Read a configured remote or an absolute local path for offline use."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        _git(cwd, "remote", "get-url", remote)
    elif not (Path(remote).is_absolute() and Path(remote).is_dir()
              and not any(char in remote for char in "\r\n\0")):
        raise RecutError("remote must be a configured name or absolute local directory")
    refs = {}
    for line in _git(cwd, "ls-remote", "--refs", remote).splitlines():
        sha, ref = line.split("\t")
        if ref in (MAIN, RELEASE, HOTFIX) or ref.startswith(("refs/tags/", PRESERVE)):
            refs[ref] = _sha(sha)
    return refs


def validate_candidate(*, cwd, head, main, branch):
    """Use the shared engine for every candidate and its hotfix scope gate.

    Adoption ratification is the pinned, trusted value shipped on main, never an
    operator input; the engine applies it only while an adoption cut is active.
    """
    try:
        return engine.compute_version(
            cwd=cwd, head=head, main=main, branch=branch,
            adoption_ratified=ratification.RATIFIED,
            standard_file=Path(ROOT) / ratification.STANDARD_FILE,
        )
    except engine.VersionError as error:
        raise RecutError(str(error)) from error


def validate_evidence(evidence, tag):
    """Validate the input contract without consulting mutable external state."""
    if not isinstance(evidence, dict) or evidence.get("tag") != tag:
        raise RecutError("missing or mismatched publication evidence")
    if evidence.get("branch") not in ("release/new", "hotfix/new"):
        raise RecutError("evidence must identify the promoted branch")
    for key in ("H", "B", "M", "squash"):
        _sha(evidence.get(key))
    if not isinstance(evidence.get("digest"), str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", evidence["digest"],
    ):
        raise RecutError("publication evidence requires an immutable digest")
    publication = evidence.get("publication")
    if not isinstance(publication, dict) or any(
        publication.get(name) is not True for name in ("pypi", "ghcr", "github", "mcp")
    ):
        raise RecutError("missing verified publication evidence")
    if evidence.get("delivery_verified") is not True:
        raise RecutError("missing verified delivery evidence")
    if evidence.get("unpromoted_heads") != []:
        raise RecutError("unreconciled RC publication evidence: unpromoted heads")
    return evidence


def plan_recut(*, refs, new_base, pending, acknowledge, pull_requests, after_hotfix):
    """Pure plan over validated facts, never silently drop pending commits."""
    old = refs[RELEASE]
    preservation = f"{PRESERVE}release-new/{old}"
    if refs.get(preservation, old) != old:
        raise RecutError("preservation ref collision")
    updates = {RELEASE: new_base}
    if preservation not in refs:
        updates[preservation] = old
    if after_hotfix:
        updates[HOTFIX] = None
    acknowledged = validate_acknowledge(acknowledge)
    ready = len(acknowledged) == len(set(acknowledged)) and set(acknowledged) == set(pending)
    return {
        "mode": "recut", "ready": ready, "expected_refs": dict(refs),
        "updates": updates, "new_base": new_base, "pending_commits": list(pending),
        "preservation_ref": preservation, "pull_requests": pull_requests,
        "replay": {"onto": new_base, "commits": list(pending),
                   "via": "reviewed pull requests with fresh checks"},
        "staging": {"branch": "release/new", "H": new_base,
                    "selection": "successful current H and digest; N=0 is not publishable"},
        "blocked": None if ready else "acknowledge the exact listed pending SHAs",
    }


def plan_hotfix(*, refs, stable, candidate):
    """Pure branch creation plan; the cut is not a promotable candidate."""
    if HOTFIX in refs:
        raise RecutError("hotfix/new already exists; reconcile it first")
    return {
        "mode": "hotfix", "ready": True, "expected_refs": dict(refs),
        "updates": {HOTFIX: stable}, "new_base": stable, "candidate": candidate,
        "pending_commits": [], "pull_requests": [],
        "staging": {"branch": "hotfix/new", "H": stable,
                    "selection": "successful current H and digest; N=0 is not publishable"},
    }


def _base(cwd, left, right):
    bases = _git(cwd, "merge-base", "--all", left, right).splitlines()
    if len(bases) != 1:
        raise RecutError("base must be unique")
    return bases[0]


def _replay_check(cwd, onto, steps):
    """Probe each replay without checking out candidate files or running hooks.

    Steps follow the old branch's first-parent line. A merge step is replayed
    against its first parent (cherry-pick -m 1 semantics), so merged pull
    requests are checked once and their side commits are not applied twice.
    """
    current = onto
    for commit in steps:
        parents = _git(cwd, "rev-list", "--parents", "-n", "1", commit).split()[1:]
        if not parents:
            raise RecutError(f"replay requires manual reconciliation: {commit}")
        result = subprocess.run([
            "git", "merge-tree", "--write-tree", f"--merge-base={parents[0]}",
            current, commit,
        ], cwd=cwd, text=True, capture_output=True)
        if result.returncode:
            raise RecutError(f"replay conflict or uncertainty at {commit}; preserve the old branch")
        tree = _sha(result.stdout.splitlines()[0])
        current = _git(cwd, "-c", "user.name=Replay validation", "-c",
                       "user.email=replay@example.invalid", "-c", "commit.gpgsign=false",
                       "commit-tree", tree, "-p", current, "-m", "chore: validate replay")


def prepare_plan(*, cwd, remote, mode, expected_main, expected_head, expected_base,
                 expected_hotfix="absent", tag=None, evidence=None, acknowledge=(),
                 pull_requests=None, old_path_runs=None):
    """Capture remote state after the caller acquires the promotion lock."""
    refuse_old_path_activity([] if old_path_runs is None else old_path_runs)
    validate_acknowledge(acknowledge)
    refs = remote_snapshot(cwd, remote)
    for ref, expected in ((MAIN, expected_main), (RELEASE, expected_head),
                          (HOTFIX, expected_hotfix)):
        wanted = None if expected == "absent" else _sha(expected)
        if refs.get(ref) != wanted:
            raise RecutError(f"stale run: {ref} moved after dispatch or lock")
    _sha(expected_base)
    if mode not in ("recut", "hotfix"):
        raise RecutError("mode must be recut or hotfix")
    if MAIN not in refs:
        raise RecutError("main is missing")
    # Tags are never forced: changed local tags fail closed. Heads stay in a
    # private namespace so local branches and the working tree remain untouched.
    _git(cwd, "fetch", "--no-write-fetch-head", "--tags", remote,
         "+refs/heads/*:refs/sdlc/heads/*")
    if remote_snapshot(cwd, remote) != refs:
        raise RecutError("stale run: remote changed during fetch")
    for ref, sha in refs.items():
        local = ref.replace("refs/heads/", "refs/sdlc/heads/", 1)
        if _git(cwd, "rev-parse", "--verify", "--end-of-options", local) != sha:
            raise RecutError(f"stale fetched ref: {ref}")
    m = refs[MAIN]
    first_parent = _git(cwd, "rev-list", "--first-parent", m).splitlines()
    git = engine.Git(cwd)
    stable, off_line = [], []
    for ref in refs:
        name = ref.removeprefix("refs/tags/")
        if ref.startswith("refs/tags/") and engine._STABLE.fullmatch(name):
            commit = _tag_commit(git, ref)
            entry = (engine._version(name), name, commit)
            (stable if commit in first_parent else off_line).append(entry)
    if not stable:
        raise RecutError("no stable tag on main's line")
    latest = max(stable)
    _, latest_tag, latest_commit = latest
    # A strict stable tag off main's first-parent line is reconciled only when
    # main already contains it (a merged historical release such as v0.6.0) and
    # it is older than the newest first-parent release. Anything else is an
    # unreconciled tag (STD-U-821) that would also skew later engine runs.
    for entry in off_line:
        if entry >= latest or not git.ancestor(entry[2], m):
            raise RecutError(f"unreconciled stable tag {entry[1]}: not on main's first-parent line")
    if mode == "hotfix":
        if expected_base != latest_commit or m != latest_commit:
            raise RecutError("hotfix base must be current stable main; adoption cannot cut hotfix")
        candidate = validate_candidate(cwd=cwd, head=latest_commit, main=m, branch="hotfix/new")
        return plan_hotfix(refs=refs, stable=latest_commit, candidate=candidate)
    if mode != "recut" or RELEASE not in refs:
        raise RecutError("recut requires an existing release/new")
    if not isinstance(tag, str) or not engine._STABLE.fullmatch(tag):
        raise RecutError("tag must be a strict stable vX.Y.Z")
    ev = validate_evidence(evidence, tag)
    if tag != latest_tag:
        raise RecutError("tag is not the current stable release on main")
    tag_ref = f"refs/tags/{tag}"
    if _git(cwd, "cat-file", "-t", refs[tag_ref]) != "tag":
        raise RecutError("release tag must be annotated")
    release = git.resolve(tag_ref)
    if ev["squash"] != release:
        raise RecutError("unreconciled tag and squash evidence")
    latest_release = next((sha for sha in first_parent if re.fullmatch(
        r"release: [0-9]+\.[0-9]+\.[0-9]+", _git(cwd, "show", "-s", "--format=%s", sha),
    )), None)
    if release != m and release != latest_release:
        raise RecutError("tag must name main head or latest release squash")
    if _git(cwd, "show", "-s", "--format=%s", release) != f"release: {tag[1:]}":
        raise RecutError("release squash subject does not match tag")
    parents = _git(cwd, "rev-list", "--parents", "-n", "1", release).split()[1:]
    if parents != [ev["B"]] or ev["M"] != ev["B"]:
        raise RecutError("unreconciled release parent/base")
    if _git(cwd, "rev-parse", f"{release}^{{tree}}") != _git(
        cwd, "rev-parse", f"{ev['H']}^{{tree}}",
    ):
        raise RecutError("squash tree does not equal reviewed H")
    branch = ev["branch"]
    source = HOTFIX if branch == "hotfix/new" else RELEASE
    if source not in refs or not git.ancestor(ev["H"], refs[source]):
        raise RecutError("reviewed H is not on the promoted branch")
    if source == HOTFIX and refs[HOTFIX] != ev["H"]:
        raise RecutError("hotfix contains unreleased commits; cannot delete it")
    if source == RELEASE and HOTFIX in refs:
        raise RecutError("active hotfix must be reconciled before normal recut")
    # Engine hotfix validation considers all tags. The just-promoted tag is
    # now newer than its base, so validate historical fixes in an isolated ref
    # view rather than weakening that current-stable gate.
    candidate = _released_candidate(cwd, ev, refs)
    if candidate["target"] != tag[1:] or candidate["B"] != ev["B"] or not candidate["promotable"]:
        raise RecutError("release does not reconcile to a promotable candidate")
    old = refs[RELEASE]
    base = _base(cwd, m, old)
    if base != expected_base:
        raise RecutError("stale base: merge base changed")
    boundary = ev["H"] if source == RELEASE else base
    exclude = (f"^{boundary}", f"^{release}")
    pending = _git(cwd, "rev-list", "--reverse", "--topo-order", old, *exclude).splitlines()
    steps = _git(cwd, "rev-list", "--reverse", "--first-parent", old, *exclude).splitlines()
    _replay_check(cwd, release, steps)
    # The recreated branch must itself be computable by the engine (main is
    # an ancestor, the cut carries the stable tag). A tag on an older release
    # squash with main advanced past it fails here instead of after the push.
    successor = validate_candidate(cwd=cwd, head=release, main=m, branch="release/new")
    if successor["base"] != tag or successor["N"] != 0:
        raise RecutError("recreated release/new does not cut at the verified tag")
    prs = [] if pull_requests is None else pull_requests
    if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
        raise RecutError("pull requests must be a JSON list of objects")
    plan = plan_recut(refs=refs, new_base=release, pending=pending, acknowledge=acknowledge,
                      pull_requests=prs, after_hotfix=source == HOTFIX)
    plan["replay"]["steps"] = steps
    plan["candidate"] = successor
    return plan


def _released_candidate(cwd, evidence, refs):
    """Recompute against the tag inventory before this verified promotion.

    A temporary bare Git view shares objects but only exposes tags reachable
    from the recorded pre-promotion main. No candidate files are executed.
    """
    objects = _git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    # System temporary storage (RUNNER_TEMP in Actions), never the checkout.
    with tempfile.TemporaryDirectory(prefix="recut-history-") as tmp:
        _git(cwd, "init", "--bare", tmp)
        Path(tmp, "objects/info/alternates").write_text(objects + "\n")
        git = engine.Git(cwd)
        for ref, sha in refs.items():
            if ref.startswith("refs/tags/") and git.ancestor(_tag_commit(git, ref), evidence["M"]):
                _git(tmp, "update-ref", ref, sha)
        return validate_candidate(cwd=tmp, head=evidence["H"], main=evidence["M"],
                                  branch=evidence["branch"])


def apply_plan(cwd, remote, plan, run=None):
    """Apply once under the external lock, using remote leases on changed refs.

    The injected runner is for offline transport tests. Production uses Git's
    atomic push. The lock, not a no-op main refspec, serializes main updates.
    """
    if not plan["ready"]:
        raise RecutError("acknowledge the exact listed pending SHAs before apply")
    if remote_snapshot(cwd, remote) != plan["expected_refs"]:
        raise RecutError("stale run: remote refs moved after planning")
    updates = plan["updates"]
    command = ["git", "push", "--atomic"]
    for ref in updates:
        command.append(f"--force-with-lease={ref}:{plan['expected_refs'].get(ref, '')}")
    command.append(remote)
    command.extend(f"{sha or ''}:{ref}" for ref, sha in updates.items())
    result = (run(command) if run else subprocess.run(
        command, cwd=cwd, text=True, capture_output=True,
    ))
    if result.returncode:
        raise RecutError("atomic push failed; reconcile remote refs before retry")
    expected = dict(plan["expected_refs"])
    for ref, sha in updates.items():
        if sha is None:
            expected.pop(ref, None)
        else:
            expected[ref] = sha
    if remote_snapshot(cwd, remote) != expected:
        raise RecutError("post-apply state uncertain; reconcile remote refs before retry")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("recut", "hotfix"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--remote", required=True)
    for field in ("main", "head", "base"):
        parser.add_argument(f"--expected-{field}", required=True)
    parser.add_argument("--expected-hotfix", default="absent")
    parser.add_argument("--tag")
    parser.add_argument("--evidence-json", default="null")
    parser.add_argument("--pull-requests-json", default="[]")
    parser.add_argument("--acknowledge", default="[]", help="JSON array of exact pending SHAs")
    parser.add_argument("--old-path-runs-json", default="[]",
                        help="JSON list of queued or in-progress old-path workflow runs")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = prepare_plan(cwd=args.repo, remote=args.remote, mode=args.mode,
                            expected_main=args.expected_main, expected_head=args.expected_head,
                            expected_base=args.expected_base, expected_hotfix=args.expected_hotfix,
                            tag=args.tag, evidence=json.loads(args.evidence_json),
                            acknowledge=json.loads(args.acknowledge),
                            pull_requests=json.loads(args.pull_requests_json),
                            old_path_runs=json.loads(args.old_path_runs_json))
        print(json.dumps(plan, indent=2), flush=True)
        if not plan["ready"]:
            return 2
        if args.apply:
            apply_plan(cwd=args.repo, remote=args.remote, plan=plan)
        return 0
    except (RecutError, engine.VersionError, ValueError, OSError) as error:
        print(f"blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
