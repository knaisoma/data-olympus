# data-olympus-release-planner routine

Status: active
Since: 2026-07-11
Schedule: cron `0 18 * * 5` (Friday 18:00 Europe/Madrid)
Purpose: turn open GitHub issues into a scoped, reviewed, operator-approved release
with implementation tickets, so the Monday cutter has a ready epic.

## What it does each run

1. Hygiene + gh auth: clean `main` checkout, `gh` reachable
   (`tooling/paperclip-gh-auth-context`).
2. Gather: `gh issue list --state open --json number,title,labels,milestone,body`.
3. Select + cluster: target 4 (range 3-5) highest-value issues, then pull in
   closely-related ones (shared files, labels, milestone, epic). Produce a Release
   Scope Brief: chosen set with rationale, explicitly-deferred set, projected bump.
4. Security clearance (MANDATORY, first phase). No release may be prepared while
   any known weakness is open. Hard rule: by the end of this phase,
   `python3 scripts/security_alerts.py` MUST exit 0 (zero open Dependabot AND zero
   open CodeQL alerts) - that is the exact gate the Monday cutter re-checks and
   blocks on (`.rules/release-routine.md` step 2a).

   Query every OPEN alert (`python3 scripts/security_alerts.py` lists them; or
   `gh api /repos/knaisoma/data-olympus/dependabot/alerts?state=open` and
   `.../code-scanning/alerts?state=open`). Drive EVERY open alert to a disposition,
   one of:
   - **Fix / dependency update** - scope it as an implementation sub-ticket in the
     release epic (the fix must land before the cutter runs); or
   - **Dismissal with a recorded justification** - only after confirming it is a
     false positive, not exploitable, or an accepted risk. Apply via:
     `gh api -X PATCH /repos/knaisoma/data-olympus/code-scanning/alerts/<n>
     -f state=dismissed -f dismissed_reason="false positive" -f
     dismissed_comment="<why, <=280 chars>"`. Valid `dismissed_reason` values are
     exactly `"false positive"`, `"won't fix"`, `"used in tests"` (Dependabot uses
     `tolerable_risk`, `inaccurate`, `not_used`, `no_bandwidth`). The comment is a
     hard cap of 280 characters.

   Dismissals are a security judgment: the planner PROPOSES each disposition, and
   the OPERATOR approves them at the approval gate below (step 6) before anything
   is dismissed. The release scope brief MUST list every open alert with its
   planned disposition (fix ticket or dismissal + reason). A release is not scoped
   until every alert has a disposition and the gate exits 0.
5. Dual-architect review: the routine (Architect, Claude Opus) drafts the scope +
   a per-issue implementation spec. The companion architect is `agy` with
   Gemini 3.5 Flash (High):
   `agy -p '<review prompt + brief>' --model 'Gemini 3.5 Flash (High)'`.
   Fallback when agy/Gemini is unavailable: codex CLI with gpt-5.6-sol high
   reasoning: `codex exec --sandbox read-only --skip-git-repo-check -m gpt-5.6-sol
   -c model_reasoning_effort="high" -C <dir> '<review prompt>'`. Iterate until both
   agree; record a `## Companion review (<agy | codex>)` evidence block
   (WF-004 / collaboration protocol).
6. Operator approval gate: request a Paperclip approval carrying the brief + specs;
   notify the operator (Telegram, GDEC-007). Wait.
7. On approval: create the release parent epic + one implementation sub-ticket per
   selected issue, each with a `Ready for Build` block, a reviewer assigned
   (GDEC-028), and `Branch: feature/<release-epic-id>` (WF-004 section 2.2 epic
   integration branch). The iterative changes-requested and re-review cycle follows WF-004 section 7 (In Review to In Progress until the reviewer approves).
   Create the `feature/<release-epic-id>` branch off `main`.

## Constraints

- No em-dashes.
- One release epic per week; the batch is expected ready by the following Monday
  (strict 1-week pipeline). The Monday cutter (`.rules/release-routine.md`) gates on
  readiness and never ships a batch that is not Done + reviewed + green.
- The epic uses the WF-004 section 2.2 shared integration branch: each feature is
  one squashed Conventional Commit per ticket on the branch, each sub-ticket updates
  the CHANGELOG `[Unreleased]` block, and the single integration MR merges to `main`
  with a MERGE COMMIT (never a squash), per `.rules/versioning.md`, so
  `compute_release.py` sees each per-feature commit. This reconciles the "release
  branch" model with STD-U-810 section 2 (no long-lived gitflow release branch).
