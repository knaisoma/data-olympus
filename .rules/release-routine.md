# data-olympus-release-cutter routine

Status: active
Since: 2026-07-01 (rewritten 2026-07-11 for staged promotion)
Schedule: cron `0 5 * * 1` (Monday 05:00 Europe/Madrid)
Human gate: a single Paperclip approval to ship after the complete release
candidate is live on kn-dev and pre-release verification is green. The routine
does not merge or promote stable before that approval.

## What it does each run (strict 1-week pipeline)

1. Resolve the current release epic (the batch the planner scoped the prior
   Friday; see `.rules/release-planning.md`). If none is ready, report and exit.
2. Readiness gate: all sub-tickets Done and reviewed, the integration branch
   `feature/<release-epic-id>` green (CI). If not ready, post blockers on the epic,
   notify the operator (Telegram, GDEC-007), and STOP. Never cut a red release.
2a. Security readiness gate (hard block): run `python3 scripts/security_alerts.py`.
    It exits 0 only when there are ZERO open Dependabot and ZERO open CodeQL
    alerts. On a non-zero exit, post the reported open-alert list on the epic,
    notify the operator, and STOP: do not build the RC. Every release must be
    clean of known weaknesses at cut time; the planner phase
    (`.rules/release-planning.md` step 4) is where they are cleared, and this gate
    confirms nothing regressed since.
3. Sync + prepare the release commit (the cutter owns the version cut; no other
   step performs it): on the integration branch, run
   `uv run python scripts/compute_release.py`; if `releasable` is false, exit
   quietly. Read `next_version` = X.Y.Z. Set the `[project]` version in
   `pyproject.toml` to `X.Y.Z`; rename the topmost CHANGELOG `[Unreleased]` block to
   `[X.Y.Z] - <UTC date>` and open a fresh empty `[Unreleased]`; write
   `docs/releases/vX.Y.Z.md` (two H2 sections `## New features` and `## Fixed`, plain
   language). Commit to the integration branch as `chore(release): vX.Y.Z`. Per-feature
   `[Unreleased]` CHANGELOG entries were already added by each sub-ticket
   (`.rules/versioning.md`). The RC built next therefore carries the final X.Y.Z.
4. Quality gates (all must pass, else open a `release blocked: <reason>` issue and
   stop): `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`,
   `bats -r tests`, a security review over `git diff <lasttag>..HEAD`, and `kb_health`.
4a. Stable version immutability readiness (hard block): run
    `python3 scripts/check_version_free.py --version X.Y.Z`. A published version
    (PyPI, the ghcr `:vX.Y.Z` image tag, or a GitHub release/tag) is immutable, so
    a non-zero exit means the cut version is already taken (exit 3) or a registry
    was unreachable and the guard failed closed (exit 4). On either, STOP: bump the
    version (re-run step 3) or wait for the registry, then re-check. The same guard
    runs in PR CI, so this is the cutter's early confirmation. A genuine
    reconcile is allowed only when every existing artifact names the same exact
    source and hash. Only the operator may override a stuck registry with
    `KB_BYPASS_VERSION_CHECK=1`.
5. Resolve `SOURCE_SHA` from the final reviewed integration head and select the
   next unused candidate number, which must be 3 or greater for 0.6.0. Dispatch
   `gh workflow run rc-publish.yml -f ref="$SOURCE_SHA" -f number="$RC_NUMBER"`.
   The workflow publishes `X.Y.ZrcN` to PyPI, the
   `ghcr.io/knaisoma/data-olympus:X.Y.Z-rc.N` image, a wheel, sdist,
   `release-provenance.json`, and the GitHub prerelease from the exact source SHA.
   The `rc` channel moves only after all candidate surfaces verify. Wait for the
   run to succeed and capture every hash and digest from the provenance asset.
6. Canary + record rollback point: read the current `:kndev` source (the stable
   version kn-dev runs) and record it (`.rules/release-rollback.md` step 0). Then
   `gh workflow run set-channel.yml -f source=X.Y.Z-rc.N`. Keel rolls the RC onto
   kn-dev within ~2 minutes.
7. Pre-release verify: `data-olympus verify --target <kn-dev ingress>` must be
   green (health, readiness, search, enforcement). If red, roll back
   (`set-channel source=<rollback point>`) and open a `release blocked` issue.
8. Approval-to-ship: request a Paperclip approval on the epic and notify the
   operator (Telegram). The operator may exercise the RC live on kn-dev, then
   approves or rejects. On rejection, roll back and stop.
9. Promote (on approval): merge the integration MR (`feature/<release-epic-id>`
   to `main`) with a MERGE COMMIT, never a squash (per `.rules/versioning.md`: each
   per-feature commit and the `chore(release)` version-cut commit must reach `main`
   individually so `compute_release.py` sees them). Wait for required CI on the
   resulting `main` SHA. Then explicitly dispatch `tag-release.yml` through
   `workflow_dispatch` with `candidate_tag=X.Y.Z-rc.N`. The workflow rejects any
   candidate other than the highest complete candidate, requires its source to
   be an ancestor of `main`, and enters the protected `pypi` environment before
   publishing stable Python artifacts. It creates `vX.Y.Z` at the candidate
   source SHA only after PyPI succeeds, then promotes the exact verified GHCR
   digest to `vX.Y.Z`, `stable`, and `latest` without rebuilding. Finally run
   `gh workflow run set-channel.yml -f source=vX.Y.Z` so kn-dev runs stable.
10. Post-release verify: `data-olympus verify --target <kn-dev ingress>` green. If
    red, roll back per `.rules/release-rollback.md` (post-release path) and notify.
11. Release note into the Paperclip task: post the `docs/releases/vX.Y.Z.md` content
    as a comment on the routine's Paperclip issue.
12. Retro to company-knowledge: run a short retro (what the RC/verify cycle
    surfaced, any gate that fired, any manual fix, kn-dev-vs-stable drift) and
    propose a lessons-learned update to company-knowledge via the data-olympus MCP
    (`kb_propose_edit` / `kb_propose_memory` targeting a data-olympus-scoped KB path
    such as a project note or an operator memory, operator-gated). This is how the
    pipeline improves release over release.
13. Hold open for the operator's external announcement: keep the Paperclip task
    open with a Telegram reminder. The release artifact ships on approval + verify;
    the held-open task is the reminder to post the announcement.

## Constraints

* No em dashes in authored prose.
* The RC image carries the final `X.Y.Z` from `pyproject.toml`; `-rc.N` is a
  registry channel tag only. See `.rules/versioning.md` and STD-U-810.
* Candidate Python artifacts use the PEP 440 version `X.Y.ZrcN` and are part of
  the public candidate transaction.
* GHCR operations run in CI through `gh workflow run`; the runtime needs
  ghcr package-write via CI's token and the `kn-dev` SSH key only for direct
  cluster inspection/rollback.
* Exactly one release epic is in flight. The pipeline is reconcilable. Rerunning
  a green `rc-publish`, `tag-release`, or `set-channel` step is idempotent.
