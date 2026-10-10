# Adoption cut runbook (W6 Task 9)

Status: prepared, not executed. This runbook is the exact sequence the operator
or the controller follows to cut `release/new` for the first time under
STD-U-821, using the adoption record ratified in amendment 1.3
([vendored copy](std-u-821-amendment-1.3.md)). Nothing in this document has been
run against GitHub, PyPI, GHCR or the cluster. Every command that writes to a
remote is marked as a write, and none of them may run with a personal token
where the step says the bot identity is required (R6).

The cut changes no published object. Tags, PyPI files, GHCR tags, GitHub
releases, `operator/laptop.md` and the live StatefulSet stay exactly as recorded
in step 1, and step 9 proves it.

## Immutable tags and the point of no return

Organization rulesets make every tag matching `v*`, `identity-v*`, `*-rc.*` and
`*-hotfix.rc.*` undeletable and unmovable. A tag with one of those names cannot
be corrected or removed once it exists, by anyone, with any identity.

- The cut itself creates no tag. The record commit `C`, the `release/new`
  branch, the placeholder and retirement commits, and the dry-run build are all
  branch commits or workflow runs. The cut stays reversible by deleting
  `release/new` and reverting `C` (see "Rollback of the cut").
- The point of no return is the first `X.Y.Z-rc.N` publication: stage 2's
  `reserve` job creates the annotated candidate tag and a draft GitHub
  prerelease, which `finalize` publishes last. `0.11.1-rc.2` is burned (see
  [the known burned candidate](rc-publication-stage.md#known-burned-candidate-0111-rc2-empty-immutable-release-and-tag-created-by-the-first-live-run-before-the-draft-first-fix)),
  so the first complete candidate is later than `0.11.1-rc.2` (see "Lessons from
the first live cut (2026-10-09)": `rc.2` to `rc.4` are burned, and `release/new`
needs at least five commits before a publishable candidate). It can only run after
  `SDLC_PIPELINE=enabled` (item 4 of the enable order). From then on that
  identity is permanent, and a mistake is corrected only by a newer candidate.
- Never create a tag matching those patterns for testing, in this repository
  or as a rehearsal of any step here. Test tags belong only in local scratch
  repositories (such as the offline fixtures in `tests/test_adoption_cut.py`),
  never on the remote.

The organization Actions policy also requires every selected action to be
pinned by a full commit SHA. Every `uses:` line in `rc-build.yml`,
`rc-publish-stage.yml` and `promote-release.yml` is a 40-character SHA with
its tag in a trailing comment (checked after the rebase onto `307186d`); any
`uses:` added to these workflows must follow the same form or the run fails
before it starts.

## How the ratification enters each stage

The engine (`scripts/sdlc_version.py`) is the only authority on whether an
adoption record and its ratification are valid. The question this section
answers is where the ratification arguments come from, because the code that
supplies them must not be controllable by the code it judges.

- The values are two pairs of constants in
  `scripts/adoption_ratification.py`, checked into `main` through a reviewed
  pull request: `RATIFIED` with `STANDARD_FILE` for amendment 1.3 (the record
  route) and `EXTENSION_RATIFIED` with `EXTENSION_STANDARD_FILE` for amendment
  1.5 (the reuse of the route after a release, see "Reusing the record route
  after a release"), plus the vendored amendments at both paths. Since
  amendment 1.5 the engine requires both ratifications for every record,
  including a first adoption. They are never workflow inputs, repository
  variables, release evidence or files from the tree of `H`.
- Stage 2 (`rc-publish-stage.yml`, `scripts/rc_verify_and_publish.py`) runs
  from a checkout of `main` through `workflow_run`. It imports the constants
  and resolves the standard file relative to that checkout. `H` is fetched as
  Git data only.
- Promotion (`promote-release.yml`, `scripts/release_record.py`) runs from the
  release squash `S`, the `main` head, whose tree equals `H` by construction.
  Its own copy is therefore the candidate's claim, not an independent check.
  When the cut carries the record, it reads the module and the standard as
  blobs of the RC's recorded `M` (the `main` that stage 2 ran from) with the
  same parser as stage 1, and ignores its checkout's copy. Promotion's own
  scripts, including that parser, still equal `H`'s, so the real boundary
  there is the code-owner review of `scripts/` on `release/new`.
- Stage 1 (`rc-build.yml`) checks out and runs the code at `H`, so its own copy
  of the module is candidate data. `scripts/rc_decide.py preflight` therefore
  reads `scripts/adoption_ratification.py` and both standard files as blobs of
  the fetched `origin/main` (the frozen `M`), parses the module with `ast` (it
  is never imported or executed, and anything other than one string literal per
  constant is refused), writes the standard blobs to `to-delete/rc-trusted/`
  (outside the uploaded artifact), and runs the engine once with them. A
  missing blob leaves no file, so the engine refuses; a leftover file from an
  earlier run is deleted first.

Stage 1 cannot be made trustworthy by this: a commit on `release/new` can
change `rc_decide.py` itself. Reading `main`'s blobs keeps an honest stage 1
consistent with stage 2 and stops a branch commit from silently changing the
ratification an honest build uses. The security boundary is stage 2, which
never runs code from `H`, recomputes the identity with `main`'s constants and
refuses any provenance that disagrees. Promotion repeats the recompute with
`M`'s values and refuses while `release/ADOPTION.json` exists at `H`. Code-owner
review on `release/new` also covers `release/` and the vendored amendments, so
neither can change without the same review as `scripts/`.

Adoption dry-run mode (`--adoption-dry-run`) is used only when preflight sees a
`workflow_dispatch` with `dry_run: true`. It evaluates the record without the
ratifications, is never promotable, and stage 2 admits only `push` builds.
Pushes and non-dry-run dispatches always use the ratified path.

## Preconditions

- This wiring is merged on `main`, and Tasks 1 to 7 are merged.
- The operator has confirmed the R1 route (adoption record) rather than the
  alternative of releasing 0.11.1 on the old path and cutting at that tag.
- The placeholder guards are merged on `main`: the `version-free-guard` job
  and `tests/test_server_json.py` accept `0.0.0+unreleased` only on
  `release/new` and `hotfix/new`, and on `main` only as a tree-equal cycle
  squash. See [placeholder-version.md](placeholder-version.md), which also
  states that `version-free-guard` and `test` must be required checks on
  `main`. A placeholder-only commit then passes CI on `release/new`.
- No open pull request needs to merge to `main` between steps 3 and 4. Any
  merge to `main` after the record commit puts `main` ahead of `C`, and
  `release/new` would have to be recut.

Shell variables used below:

```bash
REPO=knaisoma/data-olympus
IMAGE=ghcr.io/knaisoma/data-olympus
KCFG=~/kubeconfigs/kn-dev.kubeconfig
mkdir -p to-delete/adoption
```

## Step 1: record the before-state

Read-only. Save the output; step 9 compares against it.

```bash
{
  git fetch --quiet origin --tags +refs/heads/main:refs/remotes/origin/main
  echo "main $(git rev-parse refs/remotes/origin/main)"
  echo "tag-object $(git rev-parse refs/tags/v0.11.0)"
  echo "tag-commit $(git rev-parse 'refs/tags/v0.11.0^{commit}')"
  git ls-remote origin refs/tags/v0.11.0 'refs/tags/v0.11.0^{}'
  curl -fsS https://pypi.org/pypi/data-olympus/0.11.0/json \
    | jq -r '.urls[] | "pypi \(.filename) \(.digests.sha256)"' | sort
  for ref in v0.11.0 latest stable rc; do
    echo "ghcr $ref $(docker buildx imagetools inspect "$IMAGE:$ref" --format '{{.Manifest.Digest}}')"
  done
  docker buildx imagetools inspect "$IMAGE:0.11.0" >/dev/null 2>&1 \
    && echo "ghcr 0.11.0 PRESENT" || echo "ghcr 0.11.0 absent"
  gh release view v0.11.0 --repo "$REPO" --json tagName,isDraft,isPrerelease,publishedAt
  kubectl --kubeconfig "$KCFG" --insecure-skip-tls-verify -n data-olympus \
    get statefulset data-olympus-mcp -o jsonpath='{range .spec.template.spec.initContainers[*]}spec-init {.image}{"\n"}{end}{range .spec.template.spec.containers[*]}spec {.image}{"\n"}{end}'
  kubectl --kubeconfig "$KCFG" --insecure-skip-tls-verify -n data-olympus \
    get pods -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}running {.imageID}{"\n"}{end}{end}'
  grep -n 'ghcr.io/knaisoma/data-olympus@sha256' \
    ~/kn-projects/company-knowledge/operator/laptop.md
} | tee to-delete/adoption/before.txt
```

Expected values, recorded on 2026-10-08:

| Object | Value |
|---|---|
| `v0.11.0` tag object | `297820df3e870f4e89596b6effc79851e4bac336` |
| `v0.11.0` commit | `4dd97d999ad4bf22762845ca43984605059a65b8` |
| PyPI `data_olympus-0.11.0-py3-none-any.whl` | `c2a76217f08f4b5a6cd8ea1bfb1fbe1e5f14cb5b2705fba91f7fcf0408a05e60` |
| PyPI `data_olympus-0.11.0.tar.gz` | `3fe2b3487a7bfde850cdc0529da55f7421305ea69b94986491cd910a747e4a7d` |
| GHCR `v0.11.0`, `latest`, `stable`, `rc` | `sha256:95beaca8782310958084140f0e6165de638430ea8ccb3486c97a68240b8ad06e` |
| GHCR bare `0.11.0` | absent (never published) |
| Live StatefulSet, both containers | `sha256:95beaca8782310958084140f0e6165de638430ea8ccb3486c97a68240b8ad06e` |

The deployed digest line in `operator/laptop.md` is stale: it still documents
0.10.0 (`sha256:51180c16...`) while the cluster runs 0.11.0
(`sha256:95beaca8...`). That file is in `company-knowledge`, outside this
repository's write scope, and this runbook does not edit it. Correcting it is a
controller follow-up through a reviewed change in that repository.

Any difference from the table stops the runbook until it is explained.

## Step 2: open the adoption issue

Write. Use this body; fill in `A` from step 3 before opening, so the issue and
the record name the same commit. If step 3 restarts with a new `A`, edit the
issue's anchor to the new value before opening the new record pull request.

```markdown
## Adoption record for STD-U-821

- Anchor `A`: <full 40-character SHA of the main head before the record commit>
- Base: `0.11.0` (tag `v0.11.0`, object 297820df3e870f4e89596b6effc79851e4bac336,
  commit 4dd97d999ad4bf22762845ca43984605059a65b8)
- Record: `release/ADOPTION.json` = `{"anchor": "<A>", "base": "0.11.0"}`,
  added by one commit `C` whose sole parent is `A`; `release/new` is cut at `C`.

### Why main is ahead of the tag

`main` moved past `v0.11.0` under the previous process: PR #321 (hook deny
reason fixes) and the W6 release tooling (PRs #326 to #334) merged without a
release. The cut therefore carries no stable tag, which is the case amendment
1.3 covers.

### Authority

This record follows STD-U-821 amendment 1.3, ratified on 2026-10-07
(knaisoma/company-knowledge@785bb77, vendored at
docs/releases/std-u-821-amendment-1.3.md). This issue records the anchor, base
and reason the amendment requires. It grants nothing beyond the amendment: no
exception, no preview waiver and no change to any published tag, package or
image.

The record is single use. The first release deletes it in its second commit on
`release/new`, and promotion fails while it exists at the reviewed head.
```

## Step 3: add the record commit `C` to `main`

Write, through a reviewed pull request to `main`. Work in a worktree.

```bash
git fetch origin +refs/heads/main:refs/remotes/origin/main
A="$(git rev-parse refs/remotes/origin/main)"
git worktree add .worktrees/adoption-record -b chore/adoption-record "$A"
cd .worktrees/adoption-record
mkdir -p release
printf '{\n  "anchor": "%s",\n  "base": "0.11.0"\n}\n' "$A" > release/ADOPTION.json
git add release/ADOPTION.json
git commit -m "chore(release): record adoption cut"
git diff --no-ext-diff --name-status "$A" HEAD   # must print only: A  release/ADOPTION.json
git push origin chore/adoption-record
gh pr create --repo "$REPO" --base main --head chore/adoption-record \
  --title "chore(release): record adoption cut" --body-file <reviewed body file>
```

Merge rules, because the engine checks the result exactly:

- Merge with squash or rebase, never a merge commit (`C` must be a non-merge
  commit).
- Merge only while the `main` head still equals `A`. If anything else merged
  first, close the pull request, update the anchor in the adoption issue to the
  new head, and restart this step with it.

After the merge, verify on a fresh fetch:

```bash
git fetch origin +refs/heads/main:refs/remotes/origin/main
C="$(git rev-parse refs/remotes/origin/main)"
test "$(git rev-list --parents -n 1 "$C")" = "$C $A"
git diff --no-ext-diff --name-status "$A" "$C"   # only: A  release/ADOPTION.json
```

## Step 4: run the engine at `C`

Read-only, from a checkout of `main` at `C` (the trusted copies):

```bash
uv run python scripts/sdlc_version.py --head "$C" --main "$C" \
  --branch release/new \
  --adoption-ratified "$(uv run python -c 'from scripts.adoption_ratification import RATIFIED; print(RATIFIED)')" \
  --standard-file docs/releases/std-u-821-amendment-1.3.md \
  --extension-ratified "$(uv run python -c 'from scripts.adoption_ratification import EXTENSION_RATIFIED; print(EXTENSION_RATIFIED)')" \
  --extension-standard-file docs/releases/std-u-821-amendment-1.5.md
```

Expected: `candidate` `0.11.1-rc.0`, `N` 0, `promotable` false, `adoption`
`ratified`, `adoption_retired` false, `B` equal to `C`. Anything else stops the
runbook. (A local dry run of the same history on 2026-10-08 produced exactly
these values.)

## Step 5: create `release/new` at `C`

Write. Branch creation needs the identity allowed by the `release/new`
ruleset, see "Blocked items" below.

```bash
git push origin "$C:refs/heads/release/new"
```

The push starts `rc-build` at `N=0`. It must build and verify every artifact,
record `promotable: false` and publish nothing (R2). Stage 2 does nothing while
`SDLC_PIPELINE` is unset and refuses `N=0` when it is set.

## Step 6: placeholder commit and retirement commit on `release/new`

Write, each through its own reviewed pull request to `release/new`, merged by
squash with exactly these subjects, in this order:

1. `chore(release): use the unreleased version placeholder`: sets
   `pyproject.toml` `version` to `0.0.0+unreleased` (R4), plus the CI coupling
   fix from the preconditions.
2. `chore(release): retire adoption record`: `git rm release/ADOPTION.json`
   and nothing else.

Each push to `release/new` starts `rc-build`. The first yields `0.11.1-rc.1`
(not promotable, the record is still at `H`); the second yields
`0.11.1-rc.2`, promotable. That candidate was burned by the first live run
(published empty before the draft-first fix). With `rc.2` to `rc.4` burned, the
candidate number is the commit count on `release/new` since the cut, so a
publishable candidate needs at least five commits there (see the lessons
section at the end).

## Step 7: engine run and dry-run RC build

Read-only engine run against the new head, with `main`'s copies:

```bash
git fetch origin +refs/heads/main:refs/remotes/origin/main \
  +refs/heads/release/new:refs/remotes/origin/release/new
uv run python scripts/sdlc_version.py --head refs/remotes/origin/release/new \
  --main refs/remotes/origin/main --branch release/new \
  --adoption-ratified "$(uv run python -c 'from scripts.adoption_ratification import RATIFIED; print(RATIFIED)')" \
  --standard-file docs/releases/std-u-821-amendment-1.3.md \
  --extension-ratified "$(uv run python -c 'from scripts.adoption_ratification import EXTENSION_RATIFIED; print(EXTENSION_RATIFIED)')" \
  --extension-standard-file docs/releases/std-u-821-amendment-1.5.md
```

Expected: `N` equal to the number of commits on `release/new` (2 with only the
placeholder and retirement commits, which names the burned `0.11.1-rc.2`; the
published candidate needs `N` of at least 5), `B` equal to `C`, `promotable`
true, `adoption_retired` true.

Then dispatch a dry-run build (write: starts a workflow, publishes nothing):

```bash
gh workflow run rc-build.yml --repo "$REPO" --ref release/new -f dry_run=true
```

This run uses adoption dry-run mode, so its version output says
`"adoption": "unratified"` and the decision is not promotable. The ratified
path is exercised by the push builds of steps 5 and 6; keep their run links as
evidence.

## Step 8: retarget open pull requests

```bash
gh pr list --repo "$REPO" --base main --state open --json number,title,headRefName
gh pr edit <number> --repo "$REPO" --base release/new   # write, per feature PR
```

None were open when the plan was written. Release-process pull requests that
must land on `main` (none are expected during the cycle) are not retargeted.

## Step 9: verify the after-state

Rerun the step 1 block into `to-delete/adoption/after.txt` and compare:

```bash
diff to-delete/adoption/before.txt to-delete/adoption/after.txt
```

The only permitted difference is the `main` line (now `C`). Tags, PyPI files,
every GHCR digest, the GitHub release, the StatefulSet spec, the running image
IDs and the `operator/laptop.md` line must be identical. Record the digest that
is actually running from the `running` lines. Confirm that no
`0.11.1-rc.*` tag, prerelease, PyPI file or GHCR tag exists:

```bash
git ls-remote origin 'refs/tags/0.11.1-rc.*'
gh release list --repo "$REPO" --limit 5
curl -fsS -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/data-olympus/0.11.1rc2/json  # 404
docker buildx imagetools inspect "$IMAGE:0.11.1-rc.2" >/dev/null 2>&1 && echo PRESENT || echo absent
```

## First live run checklist

These have only been exercised offline or against fakes. Retain evidence for
each from the first real runs:

- Buildx multi-architecture OCI export for `linux/amd64` and `linux/arm64`
  (step 5 push build).
- The `containerimage.digest` key in the Buildx metadata file and the digest
  recorded in provenance.
- Artifact size and a successful upload within the runner's artifact limits.
- Installed-package smoke tests for the wheel and the source distribution.
- The `set-channel` digest lookup: `docker buildx imagetools inspect
  --format '{{.Manifest.Digest}}'` against a real tag (for example `v0.11.0`).
- Promotion's release body comparison, which normalizes CRLF line endings and
  trailing newlines (`_normalize_body` in `scripts/release_record.py`), against
  the body GitHub actually returns.
- The App bot identity lookup (`gh api users/<slug>[bot]`) in the promotion
  jobs.
- `docker login ghcr.io` in the stage 2 `attest` job and the registry
  attestation push (`push-to-registry: true`).
- Stage 2 draft-first publication (immutable releases), see
  [the stage 2 release ordering](rc-publication-stage.md#release-ordering-under-immutable-releases):
  - Listing `releases` with the `contents: write` `GITHUB_TOKEN` returns the
    draft with `tag_name`, `prerelease: true` and each asset's `state`.
  - The release read back right after `gh release create --draft
    --verify-tag --prerelease` is `draft: true` before `pypi` starts (the
    highest-impact check: a published release here would burn the candidate,
    as happened to `0.11.1-rc.2`).
  - `gh release upload` targets the draft by tag.
  - `gh api -H "Accept: application/octet-stream"
    repos/<repo>/releases/assets/<id>` returns the full bytes of a draft
    asset. If the run reports a differing draft asset, confirm the bytes
    before deleting anything: a correct asset must not be deleted.
  - `PATCH releases/<id>` with `draft: false`, `prerelease: true` and
    `make_latest: "false"` publishes the release, keeps it a prerelease, leaves
    Latest unchanged, and the listing shows it as published at once.
  - "Re-run all jobs" on a completed candidate skips `pypi` but still runs
    `attest` and `finalize` (as a readback).
  - The annotated candidate tag message reads back (`git/tags/<sha>`) exactly
    as written: the `Candidate`, `H:` and sorted `sha256` lines, with at most
    a trailing newline added.
  - Promotion: `gh release view`, `gh release download` and
    `gh release edit --draft=false` resolve the stable draft by tag, so a
    rerun finds the existing draft instead of creating a second one.

## Blocked items and enable order

Each item needs owner action or the W9 bot or GitHub App (R6). They are not
done by this runbook and must never be replaced by a personal token. Enable
them in this order:

1. The repository ruleset for `release/new`: pull request required, code-owner
   review for `scripts/`, `.github/`, `release/` and
   `docs/releases/std-u-821-amendment-1.3.md` (all listed in
   `.github/CODEOWNERS`). Needed before step 6 so the two
   commits land through pull requests.
2. The GitHub App installation and the `sdlc-bot` environment (deployment
   branch policy `main` only, environment secret `SDLC_APP_PRIVATE_KEY`,
   variable `SDLC_APP_ID`), plus the copies in the `pypi` environment that
   `promote-release.yml` needs.
3. The `pypi-rc` environment and its own PyPI Trusted Publisher for
   `rc-publish-stage.yml`.
4. `SDLC_PIPELINE=enabled`. From this point a push to `release/new` with a
   promotable head publishes a candidate, so enable it only after steps 1 to 9
   are verified.
5. `SDLC_RC_CHANNEL=enabled`, only after the first new-model release is
   published and verified. Until then `set-channel.yml` stays the only writer
   of `rc`. Enabling it also requires moving the stage 2 `rc` move into a job
   after `finalize` (or enabling it only together with that change), because
   today `publish` moves `rc` before the draft release is published; see
   [the `rc` channel](rc-publication-stage.md#the-rc-channel).

The adoption issue (step 2) is also an owner action.

## Rollback of the cut

The cut is reversible until the first publication, the point of no return
described above: the first stage 2 `reserve` job, which creates the first
`X.Y.Z-rc.N` tag and prerelease and can only run with `SDLC_PIPELINE=enabled`.
Until then no tag exists to undo:

1. Delete `release/new` (write, needs the identity the ruleset allows):
   `git push origin --delete release/new`.
2. Revert `C` on `main` through a reviewed pull request whose single commit
   deletes `release/ADOPTION.json` with a conventional subject such as
   `revert(release): remove adoption record` (a default `Revert "..."` subject
   does not parse under the commit grammar).
3. Comment the reason on the adoption issue and close it.

Nothing published needs rollback at this point, because nothing was published.
After the first publication the cut is no longer rolled back this way: the
candidate tag cannot be deleted or moved under the organization rulesets. Use
the documented release rollback (`.rules/release-rollback.md`), which only
redeploys recorded digests and never retags.

## Reusing the record route after a release

Amendment 1.3 grants the record route for one case, the adoption of a product
whose `main` is ahead of its last stable tag because commits landed under the
previous process, and calls the record single use. It does not cover a later
cycle. That reuse is authorized by STD-U-821 amendment 1.5, ratified on
2026-10-10 (company-knowledge #236, squash `2441459`,
[vendored copy](std-u-821-amendment-1.5.md)). The trigger is structural: stage
2 and promotion always run `main`'s definitions, so a release tooling defect
that only a real release reveals can be repaired only on `main` after the
stable tag exists, as with the release tooling repairs #358 to #361 and #364
after `0.11.1`. Amendment 1.5 limits
such commits to release tooling repairs, each a reviewed pull request. Without a
record the next cycle cannot be cut: at the untagged `main` head the engine
fails with `missing_tags_in_released_product`, and at the tag with
`recut_required`.

The engine enforces the amendment as follows, and refuses the record otherwise:

- `base` is a strict `X.Y.Z` whose `vX.Y.Z` exists and is the highest stable
  tag by SemVer precedence. A lower tag that is still an ancestor, a version
  with no tag, and anything that is not a strict `X.Y.Z` (a `v` prefix, a
  prerelease, build metadata or whitespace) are refused.
- `v<base>` is an ancestor of `B` and a strict ancestor of the anchor
  (`git rev-list --count v<base>..<anchor>` is at least 1), so the route cannot
  be used when `main` is not ahead of the tag.
- No tag in the repository that starts with `v` followed by a digit may be
  anything other than a strict stable version or a strict prerelease (for
  example `v0.11.01` is refused with `invalid_stable_tag`), so a malformed tag
  cannot hide a higher release. Tags without the `v` prefix, such as the
  candidate tags `0.11.1-rc.5`, are not version tags here.
- Both ratification lines are required: `Ratification: <date>` under
  `## Amendment 1.3` in `STANDARD_FILE`, matching `RATIFIED`, and
  `Ratification: <date>` under `## Amendment 1.5` in `EXTENSION_STANDARD_FILE`,
  matching `EXTENSION_RATIFIED`. A missing file, a line outside its section, a
  date mismatch, a future date or a value that is not `<date>:<ref>` is refused
  with `adoption_unratified`. If amendment 1.5 is revoked (its ratification line
  removed from the vendored copy, or the constant changed), the engine refuses
  every record again.
- Every other rule of amendment 1.3 stays: the record commit adds only
  `release/ADOPTION.json`, the anchor is its sole parent and has no record,
  `B` carries no stable tag, the record is deleted by the first commit on
  `release/new`, and no hotfix is cut during that cycle.

The record is single use per record, and only while its base is the highest
stable tag: once the next stable tag exists, `v<base>` is no longer the highest
and the same record is refused. A later need requires a new record and a new
adoption issue entry under amendment 1.5.

The procedure is steps 2 to 9 above with these changes. The adoption issue (one
issue or one issue update per use) records the anchor, the base tag and the
repairs that put `main` ahead of it, and cites amendment 1.5. `base` is the
highest stable tag of the product, not `0.11.0`. In step 3, find the base and
write the record (inside the worktree) with:

```bash
git fetch --tags origin +refs/heads/main:refs/remotes/origin/main
BASE_TAG="$(git tag --list 'v[0-9]*' | grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
  | sort -V | tail -n 1)"
git merge-base --is-ancestor "$BASE_TAG" refs/remotes/origin/main   # must succeed
test "$(git rev-list --count "$BASE_TAG..refs/remotes/origin/main")" -ge 1
git log --format='%h %s' "$BASE_TAG..refs/remotes/origin/main"      # the repairs, for the issue
A="$(git rev-parse refs/remotes/origin/main)"
printf '{\n  "anchor": "%s",\n  "base": "%s"\n}\n' "$A" "${BASE_TAG#v}" > release/ADOPTION.json
```

Before opening the record pull request, check that the commits between the tag
and the anchor stay within amendment 1.5 (release tooling repairs only). The
engine does not inspect this range, so the check is part of the procedure:

```bash
git diff --name-only "$BASE_TAG..$A" -- src/ pyproject.toml
```

Expected output: empty. Any listed path is a product or build change that the
amendment does not cover. Either revert it on `main` and re-land it through
`release/new` before recording the cut, or record an explicit exception, with
the decision and who made it, in the adoption issue. Do not record the cut
with an unexplained path in that list.

Expected engine output at `C` (step 4, with both ratifications as shown
there): `base` `v<base>`, `candidate` `<base with patch + 1>-rc.0` for a
pre-1.0 product, `N` 0, `promotable` false. Impact and the release notes then
cover `v<base>..H`, so the repairs on `main` appear in the next release's
notes; `N` still counts `B..H`. The first commit on `release/new` retires the
record; the placeholder is already on `main` after a new-model release, so no
placeholder commit is needed.

### Known open items

The commits on `main` after `v0.11.1` (`git log --oneline v0.11.1..origin/main`
on 2026-10-10) are #358, #359, #360, #361 and #364 (release tooling repairs),
#362 (build backend pin) and #363 and #365 (onboarding product fixes). For
them, the check above lists `pyproject.toml`,
`src/data_olympus/onboarding_inflight.py` and
`src/data_olympus/tools_onboarding.py`.

Known out-of-scope commits on `main` after `v0.11.1`: #363 and #365
(onboarding product fixes, outside amendment 1.5 scope: product changes must
use `release/new`), and #362 (build backend pin, borderline: build
configuration). The next adoption issue records the handling decided by the
operator or lead at cut time: (a) revert them on `main` and re-land through
`release/new`, or (b) a recorded exception in the issue. Nothing is blocked now
because there is no release to cut.

`tests/test_adoption_reuse.py` covers the cycle after `0.11.1` end to end
(base `0.11.1`, an anchor four commits past the tag, the record, then one fix
commit that also retires it): `0.11.2-rc.1`, promotable only when both
ratifications are given, and refused or unratified otherwise.

## Lessons from the first live cut (2026-10-09)

The first live cut was rolled back once and redone. These are the facts that
changed the procedure.

1. Land every fix on `main` before the record commit. The stage 2 workflow
   definition always comes from `main` (`workflow_run`), and any commit on
   `main` after the record commit `C` makes the engine refuse `release/new`
   (`recut_required`); under amendment 1.3 that needs a new anchor and a new
   record. The first cut was rolled back for this reason: `release/new` was
   deleted, the record removed, and a new anchor and record created. Merge all
   stage 2 and workflow fixes first, then add `C`.
2. Immutable releases stay enabled. A published release can never gain assets,
   so stage 2 creates the release as a draft, uploads and verifies the assets,
   and publishes last. The first live run published an empty release for
   `0.11.1-rc.2`, which is burned permanently: the tag and release cannot be
   deleted or moved, because tags matching `v*` and `*-rc.*` are protected.
3. Burned names and the N>=5 rule. The candidate number is the commit count on
   `release/new` since the cut. With `rc.2` to `rc.4` burned, the replay needed
   at least five commits on `release/new` before a publishable candidate.
   Stage 2 refuses a colliding candidate cleanly (`Git tag collision: different
   H`) and writes nothing. `rc.1` and the other low numbers are refused because
   they are not promotable or collide.
4. Release listing lag. Right after a draft release is created the listing may
   not show it, and the first two runs failed with `draft release was not
   created`. Both passed on one re-run of the failed job. The bounded retry
   (#348) takes effect once it lands on `main`.
5. The aggregate CodeQL check run exists only on pull request heads (app
   `github-advanced-security`) and never on pushes to `main`. The promotion
   gate therefore requires `test` and the aggregate CodeQL on the release pull
   request head `H`, and `test` plus the per-language `Analyze` checks on the
   squash `S`. The first design expected the aggregate on `S` and failed closed
   before anything was published.
6. Ruleset interaction. Creating `release/new` at a commit whose history
   contains a merge commit is blocked by the linear history rule. The
   controller disabled the ruleset only for the creation push and re-enabled it
   right after. Recut deletes and recreates the branch (no force, no lease).
7. KB consult hook. `pyproject.toml` and `uv.lock` are governed paths, so the
   commit needs an explicit consult whose workspace key is the basename of the
   main worktree (`data-olympus`), not the name of the linked worktree.
8. The `lint-title` check rejects the `release` subject type until the lint fix
   lands. The fix is tracked and was applied on `release/new` in this cycle.
