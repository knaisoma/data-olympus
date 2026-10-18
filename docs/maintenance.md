# Maintenance discovery

Maintenance runs independently of feature work. It discovers dependency,
runtime, base image and build tooling upgrades, and turns the ones that need
judgement into tracked issues that feed the normal release scope. It never
changes code on its own.

## Waiting periods and deadlines

| Update | Minimum wait | Adoption target |
|---|---|---|
| Patches, including base image digest refreshes within a line | None | As soon as possible after validation |
| Minors | 14 days after the stable release | Within 30 days of eligibility |
| Dependency majors and a new Python feature line | 60 days after general availability | Within 90 days of eligibility |
| A line approaching end of support | Not applicable | Replaced at least 30 days before support ends |

The earliest applicable security, adoption or support deadline wins. Urgent
security fixes take the expedited path with review and validation preserved.

Maturity uses three states: Observed (known, impact being assessed), Eligible
(waiting period and compatibility conditions met) and Validated (the release
candidate passed the required checks). Age alone does not make an upgrade
eligible: check support, required integrations, migration guidance and known
blockers.

## Dependabot

`.github/dependabot.yml` proposes version updates weekly for GitHub Actions,
`uv` and the Docker base image in `deploy/docker/Dockerfile`.

- `uv` uses per-SemVer cooldown: majors 60 days, minors 14 days, patches 0.
- GitHub Actions and Docker accept only one `default-days` value, with a
  minimum of 1. Actions use 14 days; a major proposed after 14 days is not
  merged before its assessment issue is Eligible. Docker uses 1 day, the
  closest available value to "no wait" for digest refreshes.
- Docker version bumps of the `python` image (a new feature line) are ignored:
  that is a runtime upgrade handled through an assessment issue below.
- Security updates ignore cooldown.

### Dependabot target branch

Version updates target `main`, like the other maintainer pull requests at
present. `release/new` is not used yet because the release tooling deletes and
recreates it after every release (`scripts/sdlc_recut.py`), which closes every
pull request based on it, and because it does not yet carry every change on
`main`. Retarget version updates to `release/new` once it is permanent.
Security updates always target the default branch whatever this setting says.

## Weekly inventory

`.github/workflows/maintenance-inventory.yml` runs every Monday on a hosted
runner with `contents: read` and `issues: write`, and runs
`scripts/maintenance_inventory.py`. It reads:

- The Python feature line in use (base image tag) and the official lifecycle
  data at <https://endoflife.date/python>.
- The pinned base image digest against the registry's current digest for the
  same tag. A refresh is a patch handled by Dependabot, so it appears only in
  the run summary.
- The direct dependencies locked in `uv.lock`, against the next major on PyPI.
  Calendar-versioned packages are skipped.
- Every action pinned by SHA with a `# vN` comment, against its next major
  release.

For each upgrade step it opens one issue titled
`maintenance: assess <name> <current> to <next> (<ecosystem>)`. The body holds
the assessment fields: current and target versions, official references,
maturity eligibility date and evidence, breaking changes and compatibility,
effects on configuration, runtime, data and integrations, prerequisite
upgrades, test and recovery plans, owner and target release, and the
security, adoption and end-of-support deadlines. Fields that need judgement
are marked "to assess" for the owner to complete.

Majors are taken one step at a time: the target is the next major, not the
latest. The eligibility date is computed from the first general availability
release of that major as published by its source.

### Deduplication

Each issue carries a hidden marker
`<!-- maintenance-key: data-olympus/<ecosystem>/<name>/<current>-to-<next> -->`.
A step whose key appears in any issue, open or closed, is never opened again.
A newer release within the same next major is the same step. Closing an issue
therefore records the decision for that step; the following step gets its own
issue.

### Dry run

Dispatching the workflow manually defaults to a dry run, which prints the
planned issues in the run summary and opens nothing. Issues are opened only by
the schedule or by a dispatch from `main` with the dry run turned off. Locally:

```bash
python -m scripts.maintenance_inventory --repo knaisoma/data-olympus --dry-run
```

A source that cannot be read is reported as an error and fails the run after
everything else has been processed. If the issue list itself cannot be read,
the run opens nothing, because deduplication would be unknown.
