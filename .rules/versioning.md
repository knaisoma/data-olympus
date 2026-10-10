# Data Olympus versioning and release rule

Status: active
Governing standard: STD-U-821 v1.2

## Adoption and transition

This is the target release contract. The new pipeline stays disabled until its
bot identity, protections, environments and adoption approvals exist. Until the
first new-model release, `rc-publish.yml`, `tag-release.yml` and `set-channel.yml`
retain their transitional behavior. See [release-routine.md](release-routine.md).

## Branches and version authority

Use one `release/new`, cut from tagged `main` after each verified release.
Feature branches start there and their PRs target it. Integrations use squash
messages carrying their full Conventional Commit impact. Release PRs must be
squash merged, preserving linear history. Only release records advance `main`.

The engine `scripts/sdlc_version.py` and the `release_artifacts.py` version
overlay are delivered separately, not yet in the repository. The engine will
be the new-model authority. No target version will be committed on
`release/new`: its first commit after the cut will set `pyproject.toml` to
`0.0.0+unreleased`, and the overlay will inject computed versions into build metadata.
The placeholder is never published or used to decide a version. Its change
lands on `release/new`, not on transitional `main`; `main` then receives it
through each tree-equal release squash. Where CI accepts the placeholder is
defined in
[docs/releases/placeholder-version.md](../docs/releases/placeholder-version.md).

Fetch full history and tags and freeze these inputs:

- `H`: exact pushed branch head.
- `M`: fetched `main` SHA.
- `B`: the unique result of `git merge-base --all M H`.

Require `M` to be an ancestor of `H`. If main advanced, preserve pending work
and recut before building. Require exactly one strict stable `vX.Y.Z` tag on
`B`; an older nearest tag or RC tag is not a base. Compute
`N = git rev-list --count B..H`, including merges. Parse complete messages of
every non-merge commit; malformed messages fail. Synthetic merge subjects
contribute no impact. `N` counts commits, never workflow runs.

## Content-derived targets

Choose the highest contribution in the parsed set:

| Contribution | Base below 1.0.0 | Base at least 1.0.0 |
|---|---|---|
| Breaking marker on any type | Minor, reset patch | Major, reset minor and patch |
| `feat` | Patch | Minor, reset patch |
| `fix` | Patch | Patch |
| Other valid types | No contribution | No contribution |

A nonempty set without a contribution floors to patch, including `perf`, docs
and dependency changes. Reverts do not subtract prior impact; rewrite their
subjects as `revert: <original subject>`. Breaking markers are `!` before the
colon or a nonempty `BREAKING CHANGE:` / `BREAKING-CHANGE:` footer, not incidental
prose. Compare each proposed squash message with all non-merge source commits;
reject lower impact and preserve breaking details. Moving from 0.x to 1.0.0
requires an explicit operator GA decision in project rules and
`release/GA-DECISION` at reviewed `H`.

At the cut, `N=0` uses the patch target and `rc.0`: build and verify, publish
nothing, and never promote. A never-released product needs a recorded bootstrap
cut and target (`0.1.0`, or explicitly approved `1.0.0`). Missing tags in an
already released product must not silently enable bootstrap.

## Candidate identities

| Branch | Canonical identity | PEP 440 metadata and wheel/sdist version |
|---|---|---|
| `release/new` | `X.Y.Z-rc.N` | `X.Y.ZrcN` |
| `hotfix/new` | `X.Y.Z-hotfix.rc.N` | `X.Y.Z.devN` |
| Stable | `X.Y.Z` | `X.Y.Z` |

Candidates have no `v`. Reserve `.devN` for hotfixes: it is an explicit mapping,
not normalization. `1.4.3-hotfix.rc.3` maps to `1.4.3.dev3`; `1.4.3-rc.3` maps
to `1.4.3rc3`. Release records retain canonical identities. OCI version labels
use the computed version and revision labels the full build SHA. Promotion
reusing an RC digest retains its RC label and records the stable alias in
provenance without mislabelling the original build.

## Release records, hotfixes and recut

After review and gates, squash the batch to `main` with subject
`release: X.Y.Z`. Its body contains generated notes grouped by breaking changes,
features, fixes and other changes, with issue/PR links and migration guidance.
This release record is not a bump input. Prove sole parent `B` and tree equality
with reviewed `H`; create immutable annotated `vX.Y.Z` on the squash commit.
Production uses stable-tag-authorized artifacts pinned by digest in reviewed
gitops changes.

Cut `hotfix/new` from the current stable tag on `main`, with a patch target
and fixes only. Features and breaking changes take the normal path; older
maintenance lines require a separately approved contract. Apply the same
review, staging, squash and tag gates, including no publication at `N=0`.
While a hotfix exists, staging selects its successful current head; otherwise
it selects the successful `release/new` head. Use recorded `H` and digest,
never version ordering or build time.

After verified release, delete and recut `release/new` from tagged `main`.
Move open PRs and replay pending work with fresh checks and review. After a
hotfix, also delete `hotfix/new`. Recompute target and count; merging main into
the old branch must not continue its counter. Serialize staging validation,
merge, tag, delivery verification and recut under the per-product promotion
lock. Recheck head and base after acquiring it and reconcile uncertain state.

## Initial adoption remains conditional

The proposed cut is ahead of `v0.11.0` and has no stable tag. The proposed
adoption record is `release/ADOPTION.json` with fields `{anchor, base}`:

- `anchor` is the main head `A` before adding the record.
- `base` is `"0.11.0"`.

The record must be absent at `A`. Commit `C` is a non-merge commit whose sole
parent is `A` and whose only change adds the record. Cut `release/new` at
`B = C`. The engine derives `B` from the merge base and reads the record only
from the tree of `B`, never from `H`; the record does not supply `B`. Acceptance
also requires no stable tag on `B` and `v0.11.0` to be the highest stable tag
and an ancestor of `B`. If `B` has a stable tag, ignore the adoption record
and apply the normal stable-base checks.

The engine does not fix the base at `0.11.0`. It takes `base` from the record
as a strict `X.Y.Z` and requires `v<base>` to exist, to be the highest stable
tag by SemVer precedence and to be an ancestor of `B`, which is the
amendment's own validity rule for a released product. A later cycle whose
`main` advanced past its last stable tag (as after `0.11.1`) uses the same
route with that tag as `base`; see "Reusing the record route after a release"
in `docs/releases/adoption-cut-runbook.md`.

For this adoption cycle, compute impact and release notes over `v<base>..H`,
but count `N` over `B..H`. The first commit on `release/new` sets the placeholder;
the second deletes the record (`chore(release): retire adoption record`).
Promotion refuses if the record remains at `H`. Do not create `hotfix/new`
during the adoption cycle.

The operator ratified the adoption amendment on 2026-10-07 (STD-U-821
amendment 1.3, vendored at `docs/releases/std-u-821-amendment-1.3.md`). Tooling
accepts the record only with the ratification pinned on `main` in
`scripts/adoption_ratification.py` and the vendored amendment, never from
workflow inputs or `H`. Stage 2 imports them from its `main` checkout; stage 1
and promotion read them as blobs of `main` (promotion: the RC's recorded `M`),
because their checkouts are `H` and the squash `S`, whose tree equals `H`.
Adoption dry-run mode is limited to dispatched dry-run builds and is never
promotable.
An adoption issue is still to be opened to record the anchor, base and reason;
neither this document nor that issue grants anything beyond the amendment.
The cut sequence is `docs/releases/adoption-cut-runbook.md`.
The cut is reversible until publication. Alternatively, release 0.11.1 on the
transitional path and cut at its stable tag.

## Transitional authority and immutability

Until the first new-model release, `scripts/compute_release.py` and committed
`pyproject.toml` remain the old path's authorities. Its pre-1.0 mapping advances
minor for features or breaking changes, patch for fixes or performance, and
at least patch for functional paths in `scripts/check_changelog.py`. Trust
its `next_version` for that path only. Prepare version, lockfile, changelog and
notes together on a branch such as `codex/release-YYYY-MM-DD`, with a squash
title representing the full batch impact. Retire old helpers through the first
cycle without breaking transitional main's 0.11.x hotfix capability.

Published versions, Git tags, GitHub releases and OCI version tags remain
immutable, including every existing release. Refuse collisions on every
surface, including mapped Python identities. Same-`H` retries verify and reuse
existing bytes; never overwrite or republish. Different content needs a new
identity. Follow [release-rollback.md](release-rollback.md) for recovery.
