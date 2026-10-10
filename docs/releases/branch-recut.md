# Verified branch recuts and hotfix cuts

`scripts/sdlc_recut.py` plans a new `release/new` after a verified release,
or cuts `hotfix/new` from current stable main. Planning fetches Git history and
checks replay conflicts without checking out or executing candidate code.
The existing publication workflows retain their transitional behavior.

## Activation and bot identity

The new `recut-release-branch.yml` and `hotfix-cut.yml` workflows accept manual
dispatches on `main` only. Each has two jobs:

* `plan` holds no secret and no environment. It refuses a ref other than
  `refs/heads/main` and refuses unless `SDLC_PIPELINE=enabled`, then lists open
  pull requests (recut) and active old-path runs and prints the plan. It never
  applies.
* `apply` runs after `plan` succeeds, in the `sdlc-bot` GitHub environment. It
  repeats the main and pipeline checks, then requires the environment
  variable `SDLC_APP_ID` and the environment secret `SDLC_APP_PRIVATE_KEY`. A
  missing or empty value fails with exactly `blocked: requires W9 bot
  identity`; there is no human-token fallback.

The `apply` job mints a short-lived W9 GitHub App installation token for each
run with `actions/create-github-app-token`, limited to this repository and to
`contents: write`. The action revokes it in its post step. The job accepts only
a token with the installation-token prefix `ghs_` that differs from the
workflow's own `GITHUB_TOKEN`; anything else fails with the same message, and
the token value is never printed. Read-only listings and the interpreter lookup
run before the token is minted, so `gh`, `jq` and `uv` never execute with it.
The token reaches Git only as a per-process header; it is never written to Git
configuration. The workflow token stays read-only.

Every action in both workflows is pinned by full commit SHA, with the release
tag in a trailing comment, and a test rejects any other reference. Updating an
action means replacing the SHA and the comment together.

Owner provisioning, not done by this change and tracked under W9 sitting 1:

* Create the `sdlc-bot` environment with a deployment branch policy that
  allows only `main` and no required reviewers. It is the single environment
  for every job that mints a W9 App token.
* Store the App id as the environment variable `SDLC_APP_ID` (it is not a
  secret) and the private key as the environment secret
  `SDLC_APP_PRIVATE_KEY`. Keep no repository or organization copy of the key.
  A repository secret is
  released to a workflow on any branch, so a modified workflow pushed to a side
  branch could use it; the environment's branch policy is enforced by GitHub,
  not by editable workflow YAML. The `GITHUB_REF` check stays as defense in
  depth.
* Install the W9 App on this repository only, with Contents write. No App
  holds a ruleset bypass. The `release/new` and `hotfix/new` rulesets require
  pull requests for updates but leave creation and deletion permitted, and
  apply uses only those two operations (see below).

Every App-token job in the SDLC workflows, including promotion, uses this one
`sdlc-bot` environment with the same variable and secret names.

## Inputs

Capture these workflow inputs before dispatch, before waiting for the shared
`data-olympus-promotion` lock:

* `main_sha`: full main commit SHA.
* `head_sha`: full `release/new` SHA; hotfix cutting also accepts `absent`.
* `base_sha`: recut's unique merge base of main and `release/new`; for a hotfix
  cut, the current highest stable tag's commit SHA.
* `hotfix_sha`: full `hotfix/new` SHA, or `absent`.

Recut additionally requires `tag` and `evidence_json`. Its optional
`acknowledge` input is a JSON array of strings containing exactly the reported
pending SHAs, without duplicates; any other JSON value is refused. The evidence
object contains:

* `tag`, `squash`, `H`, `B`, `M`, and promoted `branch`.
* `digest`, an immutable `sha256:` digest.
* `publication`, with `pypi`, `ghcr`, `github`, and `mcp` all `true`.
* `delivery_verified: true` and `unpromoted_heads: []`.

Any other evidence field is ignored. In particular, adoption ratification is
never taken from evidence: the planner passes the pinned values from
`scripts/adoption_ratification.py` and the vendored amendment to the engine.

## Checks before any change

Before planning either mode, every strict `vX.Y.Z` tag must be reconciled with
main. A strict stable tag off main's first-parent line is accepted only when
main already contains it and it is older than the newest stable tag on that
line, as with the historical `v0.6.0`, which reached main through a merge.
Any other such tag, for example one on an old `release/new` tip, refuses both
modes with `unreconciled stable tag <tag>`. A tag that does not resolve to a
commit refuses with a message naming that tag.

The newest stable tag on main's first-parent line must then be a reconciled
release in both modes: annotated, and on a commit whose subject is
`release: <version>` (which may be main's head). A lightweight tag or a tag on
an ordinary commit refuses with `release tag must be annotated: <tag>` or
`release squash subject does not match tag <tag>`, so an ad hoc tag cannot
steer hotfix numbering. Releases up to the adoption base `v0.11.0` predate the
squash model and are tagged on ordinary commits; for them a hotfix cut
requires only the annotation. Recut always requires the release subject, so it
is never planned from a pre-model tag: the first recut follows the first
new-model release.

The recut tag must be that newest stable tag, and point to main's head or
latest release squash. The squash must have the recorded sole parent, and the same tree as
reviewed `H`. The released candidate is recomputed by the engine (fixes only
for a hotfix release) and must be promotable at the tag's version. The planner
also runs the engine on the recreated branch: it must cut at the verified tag
with `N=0`. A tag on an older squash while main has advanced therefore fails
with `recut_required` before anything is pushed.

The evidence is an explicit operator attestation. The script checks its Git
consistency; it does not query registries or independently verify delivery.

## Pending work and apply

Pending normal work is the commits after promoted `H`; after a hotfix release,
it is the normal branch's commits after its merge base with main. The plan
lists those SHAs and open PRs targeting `release/new`. Sequential `merge-tree`
replay checks walk the old branch's first-parent line and refuse conflicts; a
merged pull request is checked once against its first parent, so its side
commits are listed for acknowledgement but not applied twice. Acknowledgement
permits the recut but does not replay commits or modify PRs: preserve and
replay work through reviewed PRs with fresh checks. The old head remains
reachable at `refs/heads/sdlc-preserve/release-new/<oldsha>`. A `hotfix/new`
carrying commits after the released `H` is never deleted.

Without `--apply`, the CLI prints a JSON plan and changes no remote branch.
An unacknowledged pending-work plan exits with status 2. The plan's
`operations` list shows the pushes `--apply` will make, in order.

## Apply: delete and recreate

The protected branches require pull requests for updates, and no App holds a
bypass, so apply never moves `release/new` in place. It replaces the branch by
deleting it and creating it again, which the rulesets permit. `--apply` prints
the plan, re-reads every remote ref (main, tags, `release/new`, `hotfix/new`
and preservation refs) and refuses unless all still match the plan. It then
runs these single-ref pushes, one at a time:

1. Create the preservation ref `refs/heads/sdlc-preserve/release-new/<oldsha>`
   at the old head (skipped when it already exists at that head), with
   `git push <remote> <oldsha>:<ref>`.
2. Delete `release/new` with a compare-and-swap push,
   `git push --force-with-lease=refs/heads/release/new:<oldsha> <remote> :refs/heads/release/new`.
3. Create `release/new` at the new base with
   `git push <remote> <newbase>:refs/heads/release/new`.
4. After a hotfix release, delete the verified `hotfix/new` the same way as
   step 2, only once `release/new` is recreated.

A hotfix cut is a single creation of `hotfix/new` at the stable commit.

No push uses a plain `--force`, a `+` refspec or `--atomic`. The only lease
is on a deletion, and it names exactly the deleted ref and the planned old
head. Creations carry no lease: a plain push to an absent ref is a creation.
If the ref exists at a commit that is not an ancestor, Git refuses the push.
If it exists at an ancestor, Git itself would fast-forward it; only the
rulesets, which refuse any update that is not a pull request, stop that, so
the read before each creation (which requires the ref to be absent) is the
script's own check. The plan never names `main`, a tag or any branch other
than `release/new`, `hotfix/new` and the preservation refs; apply refuses
such a ref with `unsupported ref in plan`.

Before each push the remote is read again and every ref must match what the
previous step left; after each push it is read back. A step whose read does
not match stops the run, fail closed. The backup therefore always exists
before anything is deleted, and a refused deletion leaves `release/new`
unchanged.

### Deletion is a compare-and-swap

The read immediately before the deletion is the early check: it refuses a
moved branch before anything is pushed. The deletion itself carries a lease on
the planned old head, so the server deletes `release/new` (or `hotfix/new`)
only if it still points there. A pull request merged into the branch between
that read and the push makes the server refuse the deletion, and the run stops
with `refused by its lease` and nothing deleted. The read after the deletion
catches a branch recreated immediately afterwards and stops before creating
anything.

If GitHub ever rejects a leased deletion that the rulesets would otherwise
permit, falling back to a plain deletion is an explicit, recorded decision,
taken by changing this script and this page through review, never a silent
retry. A plain deletion reopens the window above: a commit merged between the
read and the push would be deleted undetected.

### Recovery when recreation fails

If `release/new` was deleted, or may have been (the deletion push reported a
failure, or the read after it did not match or could not be made), and it is
not recreated at the new base, the script never leaves the repository without
the branch silently. Unless the branch is known to exist again, it makes
exactly one recovery push of the old head, again a plain creation,
`git push origin <oldhead>:refs/heads/release/new`. That holds when the
remote cannot be read at all, for example after a network or token failure:
a plain creation cannot overwrite a branch that diverged. The run then fails
with an error that names the old head, the backup ref and that command, and
states the outcome: the branch was restored; the push succeeded but could
not be confirmed; or the push failed and `release/new` is `ABSENT` or
unknown, in which case run the printed command. If another writer recreated
the branch in the meantime, no recovery push is made and the error names both
heads. The backup ref stays in place in every case.

### Rulesets toggled by the controller

Rules that require linear history or passing checks can also refuse the
creation of a ref whose history contains merge commits. When the recreation
of `release/new`, or a recovery push of an old head that contains merges, can
meet such a rule, the controller that dispatches the workflow toggles the
ruleset around the apply. That toggle is outside this script, which never
changes rulesets and holds no bypass.

## Limits of the lock

The guarantee is narrower than "every privileged workflow holds the lock":

* Only the deletions carry a lease, on the deleted branch. `main`, tags and
  the preservation refs are checked by the reads before and after each push,
  which only narrow the window in which they can move.
* `tag-release.yml` and `set-channel.yml` are in the `data-olympus-promotion`
  group, so they cannot run beside a recut or hotfix cut. The old-path
  `rc-publish.yml` is not. While it can run, a main or tag move can race a
  recut or hotfix cut. Both jobs list
  their queued, in-progress, waiting, requested and pending runs and refuse
  with `old-path workflow run active` when one exists. That is a point-in-time
  check, not a lock for `rc-publish.yml`: a run of it started after the
  listing is not blocked. Keep `rc-publish.yml` disabled until the old path is
  retired, or dispatch only when no such run can start. The listing still
  covers all three old-path workflows.
* GitHub keeps at most one pending run per concurrency group. A new run that
  enters `data-olympus-promotion` cancels the run already waiting, even with
  `cancel-in-progress: false`; that cancelled run may be a stage 2 publication
  or a promotion. Admitted runs claim the lock and refused runs do not: a recut
  or hotfix dispatch from a ref other than `main`, or while `SDLC_PIPELINE` is
  not `enabled`, gets a private `sdlc-branch-noop-<run id>` group, and a
  `tag-release.yml` dispatch from a ref other than `main` gets a private
  `tag-release-noop-<run id>` group, so neither can cancel anything. An admitted
  but stale dispatch still can. Two cases always enter the lock:
  `set-channel.yml` (it acts from any ref) and a `tag-release.yml` dispatch from
  `main` with a malformed `candidate_tag`.
  Before dispatching, check that no run in the group is pending. If one was cancelled, re-dispatch it after the current run
  finishes; publication is reconciled by `H` and digest, so a retry of the same
  `H` verifies and reuses what was published, and a recut is re-planned from
  fresh snapshots.

## Hotfix cuts

Hotfix cutting requires main to equal the current stable tag commit and reuses
the version engine's fixes-only mode. It refuses an existing `hotfix/new`.
Candidates use `X.Y.Z-hotfix.rc.N`; the initial `N=0` cut is never published or
promoted. The same review, publication, and delivery gates apply. Staging
selects the successful current `H` and digest, prioritizing an existing hotfix
branch. After verified hotfix delivery, recut the normal branch with the same
preservation and conflict checks.

## Prerequisites before enabling

These are tracked prerequisites for setting `SDLC_PIPELINE=enabled`; until
each is met, the workflows stay disabled:

* The `sdlc-bot` environment, its variable and secret, and the W9 App
  installation described above (W9 sitting 1).
* Publication evidence must come from facts, not attestation: either the
  Task 5 release record (tag, `H`, squash and digest) is consumed as the
  evidence source, or read-only checks confirm that the GitHub release exists
  for the tag, the GHCR tag resolves to `digest`, and the PyPI version exists.
  Until then `publication`, `delivery_verified` and `unpromoted_heads` remain
  operator-supplied and only their shape and Git consistency are checked.
* The old path is retired, or the operator accepts the race described under
  the lock limits.
