# data-olympus-release-cutter routine

Status: active
Since: 2026-07-01 (rewritten 2026-07-11 for staged promotion)
Schedule: cron `0 5 * * 1` (Monday 05:00 Europe/Madrid)
Human gate: a single Paperclip approval-to-ship after the release candidate is
live on kn-dev and pre-release verification is green. The routine then promotes;
it does not merge or publish before that approval.

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
5. Build the RC: `gh workflow run rc-publish.yml -f ref=feature/<release-epic-id>`.
   This publishes `ghcr.io/knaisoma/data-olympus:X.Y.Z-rc.N` + the `:rc` channel and
   a GitHub pre-release. Wait for the run to succeed; capture `X.Y.Z-rc.N`.
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
9. Promote (on approval): merge the integration MR (`feature/<release-epic-id>` ->
   `main`) with a MERGE COMMIT, never a squash (per `.rules/versioning.md`: each
   per-feature commit and the `chore(release)` version-cut commit must reach `main`
   individually so `compute_release.py` (which walks `git log --no-merges`) and
   `tag-release.yml` see them). `tag-release.yml` then cuts tag `vX.Y.Z`, builds the
   stable image, publishes PyPI, and creates the GitHub Release. Because a verified
   `X.Y.Z-rc.N` image exists in ghcr, `tag-release.yml` promotes it by re-tagging that
   exact digest to `vX.Y.Z` + `:latest` (byte-identical to what passed kn-dev verification),
   rather than building fresh. Then `gh workflow run set-channel.yml -f source=vX.Y.Z` so
   kn-dev runs the promoted stable.
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

- No em-dashes in authored prose.
- The RC image carries the final `X.Y.Z` (baked from `pyproject.toml`); `-rc.N` is a
  registry channel tag only. See `.rules/versioning.md` and STD-U-810.
- ghcr operations run in CI (dispatched via `gh workflow run`); the runtime needs
  ghcr package-write via CI's token and the `kn-dev` SSH key only for direct
  cluster inspection/rollback.
- Exactly one release epic in flight; the pipeline is idempotent (re-running a
  green step is safe: rc-publish, tag-release, and set-channel are all idempotent).
