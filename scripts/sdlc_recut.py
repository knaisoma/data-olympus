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
    plan = {
        "mode": "recut", "ready": ready, "expected_refs": dict(refs),
        "updates": updates, "new_base": new_base, "pending_commits": list(pending),
        "preservation_ref": preservation, "pull_requests": pull_requests,
        "replay": {"onto": new_base, "commits": list(pending),
                   "via": "reviewed pull requests with fresh checks"},
        "staging": {"branch": "release/new", "H": new_base,
                    "selection": "successful current H and digest; N=0 is not publishable"},
        "blocked": None if ready else "acknowledge the exact listed pending SHAs",
    }
    plan["operations"] = apply_operations(plan)
    return plan


def plan_hotfix(*, refs, stable, candidate):
    """Pure branch creation plan; the cut is not a promotable candidate."""
    if HOTFIX in refs:
        raise RecutError("hotfix/new already exists; reconcile it first")
    plan = {
        "mode": "hotfix", "ready": True, "expected_refs": dict(refs),
        "updates": {HOTFIX: stable}, "new_base": stable, "candidate": candidate,
        "pending_commits": [], "pull_requests": [],
        "staging": {"branch": "hotfix/new", "H": stable,
                    "selection": "successful current H and digest; N=0 is not publishable"},
    }
    plan["operations"] = apply_operations(plan)
    return plan


def _require_release_tag(cwd, refs, tag, commit, *, strict):
    """The newest stable tag on main's line must be a reconciled release.

    It must be annotated and name a `release: X.Y.Z` squash for its version.
    Releases up to the adoption base (v0.11.0 and older) predate the squash
    model and are tagged on ordinary commits, so in hotfix mode only the
    annotation is required for them. Recut is strict: a recut always follows
    a new-model release, never a historical one.
    """
    if _git(cwd, "cat-file", "-t", refs[f"refs/tags/{tag}"]) != "tag":
        raise RecutError(f"release tag must be annotated: {tag}")
    historical = engine._version(tag) <= engine._version(f"v{engine.ADOPTION_BASE}")
    if (strict or not historical) and _git(
        cwd, "show", "-s", "--format=%s", commit,
    ) != f"release: {tag[1:]}":
        raise RecutError(f"release squash subject does not match tag {tag}")


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
    _require_release_tag(cwd, refs, latest_tag, latest_commit, strict=mode == "recut")
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
    release = git.resolve(tag_ref)
    if ev["squash"] != release:
        raise RecutError("unreconciled tag and squash evidence")
    latest_release = next((sha for sha in first_parent if re.fullmatch(
        r"release: [0-9]+\.[0-9]+\.[0-9]+", _git(cwd, "show", "-s", "--format=%s", sha),
    )), None)
    if release != m and release != latest_release:
        raise RecutError("tag must name main head or latest release squash")
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


def apply_operations(plan):
    """Order the remote changes of a plan as single-ref creations and deletions.

    The protected branches allow creation and deletion but require pull requests
    for updates, and no App holds a bypass, so a branch is never moved in place:
    the backup is created first, then release/new is deleted and recreated at
    the new base, and a verified hotfix branch is deleted last.
    """
    updates, expected = plan["updates"], plan["expected_refs"]
    unknown = {ref for ref in updates if not _allowed(ref)}
    if unknown:
        raise RecutError(f"unsupported ref in plan: {sorted(unknown)[0]}")
    operations = [{"op": "create", "ref": ref, "sha": sha}
                  for ref, sha in updates.items() if ref.startswith(PRESERVE)]
    if RELEASE in updates:
        if RELEASE in expected:
            operations.append({"op": "delete", "ref": RELEASE, "sha": expected[RELEASE]})
        operations.append({"op": "create", "ref": RELEASE, "sha": updates[RELEASE]})
    if HOTFIX in updates:
        if updates[HOTFIX] is None:
            operations.append({"op": "delete", "ref": HOTFIX, "sha": expected[HOTFIX]})
        else:
            operations.append({"op": "create", "ref": HOTFIX, "sha": updates[HOTFIX]})
    deleted = set()
    for operation in operations:
        if operation["op"] == "delete":
            deleted.add(operation["ref"])
        elif operation["sha"] is None or (
                operation["ref"] in expected and operation["ref"] not in deleted):
            raise RecutError(f"plan creates an existing ref: {operation['ref']}")
    return operations


def _push(cwd, run, command):
    if run:
        return run(command)
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


def _allowed(ref):
    return ref in (RELEASE, HOTFIX) or ref.startswith(PRESERVE)


def _apply_command(remote, operation):
    """Single-ref pushes: never a plain --force, a "+" refspec or --atomic.

    A deletion is a compare-and-swap: its lease names the deleted ref and the
    planned old head, so the server refuses it if the branch moved. A creation
    is a plain push with no lease. Git refuses it when the ref exists at a
    commit that is not an ancestor; at an ancestor Git would fast-forward the
    ref, and only the protected-branch rules, which refuse any update that is
    not a pull request, stop that.
    """
    ref, sha = operation["ref"], operation["sha"]
    if not _allowed(ref):  # second line of defence behind apply_operations
        raise RecutError(f"unsupported ref in plan: {ref}")
    if operation["op"] == "delete":
        return ["git", "push", f"--force-with-lease={ref}:{sha}", remote, f":{ref}"]
    return ["git", "push", remote, f"{sha}:{ref}"]


def _branch(ref):
    return ref.removeprefix("refs/heads/")


def _require_state(cwd, remote, expected, message):
    if remote_snapshot(cwd, remote) != expected:
        raise RecutError(message)


UNREADABLE = "UNREADABLE"  # a remote head that could not be read


def _read_head(cwd, remote, ref):
    """The remote head of ref, None when absent, UNREADABLE when the read fails."""
    try:
        return remote_snapshot(cwd, remote).get(ref)
    except RecutError:
        return UNREADABLE


def _recover(cwd, remote, run, ref, old, error, *, backup, certain=True):
    """Recreate a branch this run deleted, at its old head, with one plain push.

    Recreating at the old head is a plain creation, like the recut itself, so
    it needs no force and cannot overwrite a branch that diverged. It is tried
    once, also when the remote cannot be read (network or token failure). The
    error always names the old head, the backup ref and the exact command.
    """
    name, manual = _branch(ref), f"git push {remote} {old}:{ref}"
    deleted = "was deleted" if certain else "may have been deleted"
    context = f"{name} {deleted} by this run and the recut did not complete ({error})"
    facts = (f"Old head: {old}. Backup ref: {backup or 'none'}. "
             f"Recover with: {manual}")
    head = _read_head(cwd, remote, ref)
    if head not in (None, UNREADABLE):
        raise RecutError(f"{context}; {name} now exists at {head}, so no recovery push was "
                         f"made. Reconcile before retry. {facts}") from error
    result = _push(cwd, run, ["git", "push", remote, f"{old}:{ref}"])
    head = _read_head(cwd, remote, ref)
    if not result.returncode and head == old:
        raise RecutError(f"{context}; the recovery push restored {name} at old head {old}. "
                         f"Reconcile before retry. {facts}") from error
    if not result.returncode and head == UNREADABLE:
        raise RecutError(f"{context}; the recovery push reported success but the remote "
                         f"could not be read to confirm it. {facts}") from error
    state = {None: "ABSENT", UNREADABLE: "UNKNOWN (remote unreadable)"}.get(head, f"at {head}")
    raise RecutError(f"{context}; the recovery push failed and {name} is {state}. "
                     f"{facts}") from error


def _apply_operation(cwd, remote, run, operation, expected, attempted):
    ref, sha, name = operation["ref"], operation["sha"], _branch(operation["ref"])
    # The read immediately before the push: every ref must still be exactly as
    # planned (or as the previous step left it), including main and tags.
    _require_state(cwd, remote, expected,
                   f"stale run: remote refs moved before {operation['op']} {name}")
    attempted.add(ref)
    result = _push(cwd, run, _apply_command(remote, operation))
    if operation["op"] == "delete":
        if result.returncode:
            head = remote_snapshot(cwd, remote).get(ref)
            if head == sha:
                raise RecutError(f"deletion of {name} refused; it is unchanged at {sha}")
            if head is not None:
                raise RecutError(f"deletion of {name} refused by its lease: it moved from "
                                 f"{sha} to {head}; nothing was deleted")
            raise RecutError(f"deletion of {name} failed and its state is uncertain; "
                             f"old head {sha}")
        expected.pop(ref)
        # The read right after: the deletion is confirmed, nothing else moved.
        _require_state(cwd, remote, expected,
                       f"deletion of {name} not confirmed; old head {sha}")
        return
    if result.returncode:
        raise RecutError(f"creation of {name} at {sha} was refused")
    expected[ref] = sha
    _require_state(cwd, remote, expected,
                   f"post-apply state uncertain after creating {name}; reconcile remote "
                   "refs before retry")


def apply_plan(cwd, remote, plan, run=None):
    """Apply once under the external lock as guarded single-ref pushes.

    Before each push the remote is re-read and every ref (main, tags, release,
    hotfix, preservation) must match what the previous step left; afterwards it
    is read back. A deletion also carries a lease on the planned old head, so
    a branch that moves between the read and the push is not deleted.
    A branch deleted here and not recreated is restored at its old head by one
    recovery push. The injected runner, for offline tests, receives each push.
    """
    if not plan["ready"]:
        raise RecutError("acknowledge the exact listed pending SHAs before apply")
    operations = apply_operations(plan)
    expected = dict(plan["expected_refs"])
    backup = plan.get("preservation_ref")
    _require_state(cwd, remote, expected, "stale run: remote refs moved after planning")
    recreated = {operation["ref"] for operation in operations if operation["op"] == "create"}
    awaiting = {}  # branches deleted by this run whose recreation has not succeeded
    attempted = set()  # refs whose push was sent
    for operation in operations:
        try:
            _apply_operation(cwd, remote, run, operation, expected, attempted)
        except RecutError as error:
            for ref, old in awaiting.items():
                _recover(cwd, remote, run, ref, old, error, backup=backup)
            # A deletion that was sent but failed, was not confirmed, or could
            # not be read back may still have taken effect; a branch that should
            # be recreated is never left absent.
            ref = operation["ref"]
            if (operation["op"] == "delete" and ref in recreated and ref in attempted
                    and _read_head(cwd, remote, ref) in (None, UNREADABLE)):
                _recover(cwd, remote, run, ref, operation["sha"], error, backup=backup,
                         certain=False)
            raise
        if operation["op"] == "delete":
            awaiting[operation["ref"]] = operation["sha"]
        else:
            awaiting.pop(operation["ref"], None)


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
