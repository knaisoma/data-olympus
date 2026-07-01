# data-olympus-release-cutter routine

Status: active
Since: 2026-07-01
Schedule: cron `0 5 * * *` (05:00 UTC daily)
Human gate: operator merges the release PR this routine opens. The routine never tags.

## What it does each run

1. Fetch `origin/main`. If HEAD is already on a `v*` tag, exit quietly (no work).
2. Run `python3 scripts/compute_release.py`. If `releasable` is false, exit quietly.
3. Quality gates (all must pass, else open a GitHub issue titled
   "release blocked: <reason>" and stop, do not open a release PR):
   - `uv run pytest -v`
   - `uv run ruff check .`
   - `uv run mypy src`
   - `bats -r tests`
   - a security review pass over `git diff <lasttag>..HEAD` (security-review skill)
   - `kb_health` sanity check
4. Read `next_version` and the `changes` buckets from the compute step.
5. On a branch `chore/release-v<next_version>`:
   - set `pyproject.toml` version to `<next_version>`
   - in `CHANGELOG.md`, rename the `[Unreleased]` block to `[<next_version>] - <UTC date>`
     and open a fresh empty `[Unreleased]` block above it
   - write `docs/releases/v<next_version>.md` with exactly two H2 sections,
     `## New features` and `## Fixed`, rewritten from the hand-written
     `[Unreleased]` prose weighed against the commit `changes` buckets, in plain
     language a non-technical reader understands. Omit a section if empty.
6. Open a PR `chore/release-v<next_version>` -> `main`, title
   `chore(release): v<next_version>`, label `release`. The PR body contains the
   `docs/releases/v<next_version>.md` content followed by an
   `## Announcement (draft)` block: a short upbeat paragraph, the two most
   important items, and `<release-url-once-published>` as a placeholder link.
7. Surface the PR to the operator as a Claude task for review. Stop. Do not merge,
   do not tag.

## Constraints

- No em-dashes in any authored prose.
- Never publish to any external platform; the announcement is a draft in the PR body.
- Never run `git tag`; tagging is `tag-release.yml`'s job after the operator merges.
- One open release PR at a time: if `chore/release-v*` is already open, update it.
