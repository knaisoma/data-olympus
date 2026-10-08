# Verified branch recuts and hotfix cuts

`scripts/sdlc_recut.py` plans a new `release/new` after a verified release,
or cuts `hotfix/new` from current stable main. Planning fetches Git history and
checks replay conflicts without checking out or executing candidate code.
The existing publication workflows retain their transitional behavior.

The new `recut-release-branch.yml` and `hotfix-cut.yml` workflows accept manual
dispatches on `main` only. They remain disabled until `SDLC_PIPELINE=enabled`
and `SDLC_BOT_TOKEN` is provisioned for the W9 bot identity. Missing either
fails with `blocked: requires W9 bot identity`; there is no human-token fallback.
The apply step also refuses a bot secret equal to the workflow's own
`GITHUB_TOKEN`. The bot credential exists only in the apply step's process
environment, as a per-process Git header; the workflow token is read-only and
used only to list pull requests. Whoever provisions `SDLC_BOT_TOKEN` must store
the W9 bot or GitHub App credential there and never a personal token; the
workflow cannot tell a machine account's token from a person's.

Capture these workflow inputs before dispatch, before waiting for the shared
`data-olympus-promotion` lock:

* `main_sha`: full main commit SHA.
* `head_sha`: full `release/new` SHA; hotfix cutting also accepts `absent`.
* `base_sha`: recut's unique merge base of main and `release/new`; for a hotfix
  cut, the current highest stable tag's commit SHA.
* `hotfix_sha`: full `hotfix/new` SHA, or `absent`.

Recut additionally requires `tag` and `evidence_json`. Its optional
`acknowledge` input is a JSON array containing exactly the reported pending SHAs,
without duplicates. The evidence object contains:

* `tag`, `squash`, `H`, `B`, `M`, and promoted `branch`.
* `digest`, an immutable `sha256:` digest.
* `publication`, with `pypi`, `ghcr`, `github`, and `mcp` all `true`.
* `delivery_verified: true` and `unpromoted_heads: []`.

Any other evidence field is ignored. In particular, adoption ratification is
never taken from evidence: the planner passes the pinned values from
`scripts/adoption_ratification.py` and the vendored amendment to the engine.

This evidence is an explicit operator attestation. The script checks its Git
consistency; it does not query registries or independently verify delivery.
The recut tag must be annotated, the highest stable tag on main's first-parent
line, and point to main's head or latest release squash. The squash must have
the recorded sole parent and the same tree as reviewed `H`. Unreconciled tags,
unpromoted candidates, or missing publication evidence block the operation.
The planner also runs the engine on the recreated branch: it must cut at the
verified tag with `N=0`. A tag on an older squash while main has advanced
therefore fails with `recut_required` before anything is pushed.

Pending normal work is the commits after promoted `H`; after a hotfix release,
it is the normal branch's commits after its merge base with main. The plan
lists those SHAs and open PRs targeting `release/new`. Sequential `merge-tree`
replay checks walk the old branch's first-parent line and refuse conflicts; a
merged pull request is checked once against its first parent, so its side
commits are listed for acknowledgement but not applied twice. Acknowledgement permits
the recut but does not replay commits or modify PRs: preserve and replay work
through reviewed PRs with fresh checks. The old head remains reachable at
`refs/heads/sdlc-preserve/release-new/<oldsha>`.

Without `--apply`, the CLI prints a JSON plan and changes no remote branch.
An unacknowledged pending-work plan exits with status 2. `--apply` prints the
plan, rechecks remote refs, then atomically replaces `release/new`, creates its
preservation ref, and removes a verified hotfix branch when applicable. Atomic
replacement avoids the gap of separate delete and create pushes. Changed refs
use explicit leases; unchanged main and tags rely on every privileged workflow
honoring the shared lock. A no-op main refspec is not a compare-and-swap guard.

Hotfix cutting requires main to equal the current stable tag commit and reuses
the version engine's fixes-only mode. Candidates use `X.Y.Z-hotfix.rc.N`; the
initial `N=0` cut is never published or promoted. The same review, publication,
and delivery gates apply. Staging selects the successful current `H` and digest,
prioritizing an existing hotfix branch. After verified hotfix delivery, recut
the normal branch with the same preservation and conflict checks.
