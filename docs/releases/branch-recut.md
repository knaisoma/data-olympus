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
* Install the W9 App on this repository only, with Contents write, and allow
  it, and nothing else, to bypass the `release/new` and `hotfix/new`
  protections for replacement and deletion.

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

The recut tag must be annotated, the highest stable tag on main's first-parent
line, and point to main's head or latest release squash. The squash must have
the release subject for the tag, the recorded sole parent, and the same tree as
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
An unacknowledged pending-work plan exits with status 2. `--apply` prints the
plan, rechecks remote refs, then runs one `git push --atomic` that replaces
`release/new`, creates its preservation ref, and removes a verified hotfix
branch when applicable. Atomic replacement avoids the gap of separate delete
and create pushes. Each updated ref carries its own `--force-with-lease` with
the expected value from the snapshot (empty for a new preservation ref); there
is no plain force. After the push the remote refs are read back and compared.

## Limits of the lock

The guarantee is narrower than "every privileged workflow holds the lock":

* `main` and tags carry no lease in the push. The apply-time snapshot only
  narrows the window in which they can move.
* The old-path workflows `tag-release.yml`, `rc-publish.yml` and
  `set-channel.yml` are not in the `data-olympus-promotion` group. While they
  can run, a main or tag move can race a recut or hotfix cut. Both jobs list
  their queued, in-progress, waiting, requested and pending runs and refuse
  with `old-path workflow run active` when one exists. That is a point-in-time
  check, not a lock: an old-path run started after it is not blocked. Keep
  `SDLC_PIPELINE` unset, and these workflows disabled, until the old path is
  retired, or dispatch only when no old-path run can start.
* GitHub keeps at most one pending run per concurrency group. A new dispatch of
  any workflow in `data-olympus-promotion` cancels the run already waiting, even
  with `cancel-in-progress: false`; that cancelled run may be a stage 2
  publication or a promotion. Before dispatching, check that no run in the
  group is pending. If one was cancelled, re-dispatch it after the current run
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

* The `sdlc-bot` environment, its two environment secrets and the W9 App
  installation described above (W9 sitting 1).
* Publication evidence must come from facts, not attestation: either the
  Task 5 release record (tag, `H`, squash and digest) is consumed as the
  evidence source, or read-only checks confirm that the GitHub release exists
  for the tag, the GHCR tag resolves to `digest`, and the PyPI version exists.
  Until then `publication`, `delivery_verified` and `unpromoted_heads` remain
  operator-supplied and only their shape and Git consistency are checked.
* The old path is retired, or the operator accepts the race described under
  the lock limits.
