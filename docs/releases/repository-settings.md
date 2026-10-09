# Repository settings the release pipeline requires

This is a checklist of the GitHub repository and organization settings that the
release pipeline of `knaisoma/data-olympus` depends on. Each item names the
setting, why the pipeline needs it, and a read-only `gh api` command that reads
it back. The page lists names only: it never holds a secret value, and the
secret read-backs below return secret names, not contents.

Status wording used below:

- Observed 2026-10-09 by the release lane: the release lane saw the setting in
  that state on that date. It is an observation, not a re-verification by the
  author of this page.
- Read-back command: the command that lets an operator check the setting now.
  Nothing on this page claims that the command was run for this page.
- Not readable from GitHub: the setting lives elsewhere (pypi.org) and has to be
  checked there.

Every command is a `GET` request. Run them with an identity that can read the
repository settings; some (environments, rulesets, installations) need admin
read access and return 403 or 404 otherwise.

```bash
REPO=knaisoma/data-olympus
ORG=knaisoma
```

## 1. Immutable releases

- Setting: GitHub immutable releases are ENABLED and MUST stay enabled.
- Why: a published release can no longer gain, change or lose assets, and its
  tag is locked. Stage two and promotion therefore create releases as drafts,
  upload every asset, read them back, and publish last. See
  [Release ordering under immutable releases](rc-publication-stage.md#release-ordering-under-immutable-releases).
  Disabling the setting would hide ordering mistakes that the pipeline is built
  to make impossible.
- Status: observed 2026-10-09 by the release lane (enabled).
- Read-back command:

```bash
gh api repos/$REPO/immutable-releases
```

- Follow-up: this setting is not part of the organisation's desired state yet.
  It is an expected setting here, and adding it to the organisation's desired
  state is a tracked follow-up.

## 2. Repository variables

Repository variables are plain configuration. None of them is a secret.

| Variable | Expected value | Notes |
|---|---|---|
| `SDLC_PIPELINE` | `enabled` | Gate for stage two, promotion, recut and hotfix cut. Switch only while no `rc-build` run is in progress, see [activation prerequisites](rc-publication-stage.md#activation-prerequisites-must). |
| `SDLC_REQUIRED_CODEQL_CHECKS` | `Analyze (actions),Analyze (javascript-typescript),Analyze (python)` | Comma separated, no spaces around the commas. Unset or malformed fails promotion closed. |
| `SDLC_RC_CHANNEL` | unset | Stays unset until the first new-model release is published. Enabling it requires moving the `rc` move into a job after `finalize`, see [the `rc` channel](rc-publication-stage.md#the-rc-channel). |

`SDLC_APP_ID` is deliberately NOT a repository variable. It is an environment
variable of `sdlc-bot` and of `pypi` (section 3).

- Status: observed 2026-10-09 by the release lane.
- Read-back commands:

```bash
gh api repos/$REPO/actions/variables --jq '.variables[] | [.name, .value] | @tsv'
gh api repos/$REPO/actions/variables/SDLC_RC_CHANNEL   # expect 404 until enabled
gh api repos/$REPO/actions/secrets --jq '.secrets[].name'   # expect no SDLC_APP_PRIVATE_KEY
```

The last command must not list `SDLC_APP_PRIVATE_KEY`: a repository or
organization Actions secret is released to workflows on every branch a writer
can push, so the key lives only in environments.

## 2a. Automatic branch deletion

- Setting: `delete_branch_on_merge` MUST be `false`.
- Why: it was `true` on 2026-10-09, and GitHub auto-deleted `release/new` right
  after the release squash pull request (#349) merged. That removed the release
  branch the release flow still needs after the merge: the next cycle,
  verification of the merged head, and the backup of pending work. The
  controller set it to `false` the same day. `release/new` and `hotfix/new`
  must never be auto-deleted.
- Status: observed 2026-10-09 by the release lane (was `true`, set to `false`
  by the controller).
- Read-back command (must print `false`):

```bash
gh api repos/$REPO --jq .delete_branch_on_merge
```

## 3. Environments

Each environment restricts deployments to `main` through a custom deployment
branch policy. A `workflow_run` or dispatched job runs with the `main` ref, so
the policy admits the workflow on `main` and rejects a definition from any other
branch.

| Environment | Branch policy | Required reviewers | Variables | Secrets |
|---|---|---|---|---|
| `pypi` | `main` only | exactly one | `SDLC_APP_ID` (own copy) | `SDLC_APP_PRIVATE_KEY` (own copy) |
| `pypi-rc` | `main` only | none | none | none |
| `sdlc-bot` | `main` only | none | `SDLC_APP_ID` | `SDLC_APP_PRIVATE_KEY` |

- Why `pypi` has its own copy: a job binds only one environment, and
  `publish-pypi` in `promote-release.yml` rechecks alerts and the tag after
  approval. Rotate both copies of the key together.
- Why `sdlc-bot` has no reviewers: its jobs run unattended (recut, hotfix cut,
  the promotion proof and tag creation). The real approval is the `pypi`
  environment's reviewer.
- Status: observed 2026-10-09 by the release lane.
- Read-back commands:

```bash
for E in pypi pypi-rc sdlc-bot; do
  echo "== $E"
  gh api repos/$REPO/environments/$E \
    --jq '{branch_policy: .deployment_branch_policy, rules: [.protection_rules[] | {type, reviewers: (.reviewers // [] | length)}]}'
  gh api repos/$REPO/environments/$E/deployment-branch-policies --jq '.branch_policies[] | [.type, .name] | @tsv'
  gh api repos/$REPO/environments/$E/variables --jq '.variables[].name'
  gh api repos/$REPO/environments/$E/secrets --jq '.secrets[].name'
done
```

The secrets listing returns names and timestamps only.

## 4. PyPI Trusted Publishers

These are configured on pypi.org for the project `data-olympus` and are NOT
readable from GitHub. Check them in the project's publishing settings on
pypi.org.

| Workflow file | Environment | Purpose |
|---|---|---|
| `rc-publish-stage.yml` | `pypi-rc` | Release candidate upload by stage two |
| `promote-release.yml` | `pypi` | Stable upload by reviewed promotion |

Owner `knaisoma`, repository `data-olympus` for both. PyPI matches repository
and workflow file name, not the ref, which is why each publisher pins an
environment and each environment is limited to `main`.

The publishers of the R9 old path (`publish-pypi.yml`, `rc-publish.yml`,
`tag-release.yml`, environment `pypi`) stay registered until adoption
completes, see [reviewed promotion](reviewed-promotion.md#prerequisites-blocked-until-w9).

- Status: not readable from GitHub.
- Read-back command: none from GitHub. The GitHub side of the pin is the
  environment read-back in section 3.

## 5. Rulesets

Read all rulesets, including inherited organization rulesets, then read each one
by id:

```bash
gh api "repos/$REPO/rulesets?includes_parents=true" --jq '.[] | [.id, .name, .target, .source_type, .enforcement] | @tsv'
gh api repos/$REPO/rulesets/<id>
gh api orgs/$ORG/rulesets --jq '.[] | [.id, .name, .target, .enforcement] | @tsv'
```

Status for the three rulesets below: observed 2026-10-09 by the release lane.

### 5.1 `main` protection

- Pull request required.
- Required status checks: `test` and `version-free-guard`. Without them a pull
  request that carries the placeholder version into `main` by another route
  could merge with a red guard, see [placeholder version](placeholder-version.md#required-checks).
- Read-back command:

```bash
gh api repos/$REPO/rules/branches/main
```

### 5.2 `w10-release-branches`

Targets `release/new` and `hotfix/new`.

- Pull request required, with code owner review and a required approving review
  count of 1. Code owner paths are `.github/`, `scripts/`, `release/` and
  `docs/releases/std-u-821-amendment-1.3.md`, listed in `.github/CODEOWNERS`.
- Merge method: squash only.
- Linear history required.
- Required status checks: `test` and `version-free-guard`.
- Bypass: a team, in `pull_request` mode only.
- No deletion rule, on purpose: recut deletes and recreates the branch.
- Creation is not restricted. Linear history does block creating the branch at
  a history that contains merge commits, so the controller toggles the linear
  history rule for that one push and restores it afterwards. After any recut,
  read the ruleset back and confirm linear history is on again.
- Read-back commands:

```bash
gh api repos/$REPO/rules/branches/release/new
gh api repos/$REPO/rules/branches/hotfix/new
```

### 5.3 `w10-protected-tags`

Targets the tag patterns `v*`, `identity-v*`, `*-rc.*` and `*-hotfix.rc.*`.

- Deletion, non-fast-forward and update are blocked, so a tag with one of
  those names cannot be moved or removed once it exists, see
  [the adoption cut runbook](adoption-cut-runbook.md#immutable-tags-and-the-point-of-no-return).
- Creation is NOT restricted. A creation rule cannot exempt
  `github-actions[bot]`, which creates the candidate tags and the `v*` tags of
  the old `tag-release.yml`.
- Tracked follow-up: restrict creation for stable tags only, with the `sdlc-bot`
  App as bypass actor, once the old path is retired. Until then the tagger check
  in promotion is an audit check and not an access control, see
  [tracked prerequisites](reviewed-promotion.md#tracked-prerequisites).
- Read-back command (tag rulesets are not returned by `rules/branches`):

```bash
gh api repos/$REPO/rulesets/<id>   # id from the listing above; check conditions.ref_name.include and rules[].type
```

## 6. Code scanning and Dependabot

- Code scanning default setup is on. The aggregate `CodeQL` check exists only
  on pull request heads, reported by `github-advanced-security`. Push-time runs
  on `main` are the three analyses `Analyze (python)`, `Analyze (actions)` and
  `Analyze (javascript-typescript)`. This is why `SDLC_REQUIRED_CODEQL_CHECKS`
  names those three and why the release head `H` needs only `test` while the
  squash `S` needs `test`, `CodeQL` and each analysis.
- Dependabot alerts are enabled, and zero open alerts is a promotion
  precondition (`scripts/security_alerts.py`). Promotion fails closed when an
  alert is open or unreadable.
- Status: observed 2026-10-09 by the release lane.
- Read-back commands:

```bash
gh api repos/$REPO/code-scanning/default-setup
gh api repos/$REPO/vulnerability-alerts -i | head -1   # 204 means enabled
gh api "repos/$REPO/dependabot/alerts?state=open" --jq 'length'   # expect 0
gh api "repos/$REPO/code-scanning/alerts?state=open" --jq 'length'
```

## 7. GitHub App

- Installed on this repository only (selected repositories, not all).
- Repository permissions: contents write, Dependabot alerts read, code scanning
  alerts read, and nothing else. `GITHUB_TOKEN` cannot read those alerts, which
  is why promotion mints an App token with no human token fallback.
- The App pushes the stable tag and is the identity the tag creation follow-up
  in section 5.3 would name as bypass actor.
- Status: observed 2026-10-09 by the release lane.
- Read-back command (needs organization admin access):

```bash
gh api orgs/$ORG/installations --jq '.installations[] | {app_slug, repository_selection, permissions}'
gh api orgs/$ORG/installation/repositories 2>/dev/null || true
```

The second command is not available to every token. The repository selection
shown by the first command is authoritative: it must read `selected`, and the
installation settings page lists the single repository.

## 8. Actions policy

- Allowed actions: selected actions only.
- SHA pinning required, so an action referenced by tag or branch is refused.
  The workflows pin by full commit SHA with the tag in a trailing comment.
- Status: observed 2026-10-09 by the release lane.
- Read-back commands:

```bash
gh api repos/$REPO/actions/permissions   # allowed_actions and sha_pinning_required
gh api repos/$REPO/actions/permissions/selected-actions
gh api repos/$REPO/actions/permissions/workflow   # default_workflow_permissions
```

## Open items

- Keep `delete_branch_on_merge` at `false` (section 2a); re-read it after any
  repository settings change.
- Add immutable releases to the organisation's desired state (section 1).
- Restrict creation of stable tags with the `sdlc-bot` App as bypass actor
  (section 5.3), after the old `tag-release.yml` path is retired.
- Set `SDLC_RC_CHANNEL=enabled` only after the first new-model release and the
  `rc` move has been placed after `finalize` (section 2).
- Remove the old-path Trusted Publishers on pypi.org only after adoption
  completes (section 4).
