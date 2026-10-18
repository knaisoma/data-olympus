# Release scope record

Before a release cycle, the operator approves an enumerated scope. In this
repository the scope is one issue opened from the **Release scope** issue form
(`.github/ISSUE_TEMPLATE/release-scope.yml`). Every feature, improvement, bug,
maintenance upgrade and migration cleanup task in the cycle is a tracked issue
here and is listed in that record.

## Fields

The form records:

- Application and release cycle.
- Released baseline version and revision (the full commit SHA of the stable tag).
- Numbered, linked work items.
- Mandatory or optional classification per item.
- Acceptance criteria per item.
- Dependencies between items.
- Intended effects and authorized risk boundaries.
- Approving operator and approved scope revision.
- Later scope decisions and their reasons.

The scope counts as approved only when the operator has filled in the approving
operator and the scope revision. A blank approval is not authorization.

## Labels

The label set is in `.github/labels.yml`:

- `mandatory`: in scope; an incomplete mandatory item blocks the release.
- `optional`: in scope; may be deferred under the rule below.
- `deferred`: taken out of a scope with a recorded reason; the issue stays open.
- `migration-cleanup`: removes a temporary migration path, shim or old workflow.
- `release-required`: must ship in the next release because of a security,
  maintenance or support deadline.

The labels are created in the repository settings by a maintainer; the file is
the documented set, not an automatic sync.

## Scope changes

Automation may defer an optional item only when its exclusion does not
invalidate mandatory acceptance criteria, included dependencies, security or
maintenance deadlines, or the approved risk boundaries. It records the reason
under "Later scope decisions", labels the issue `deferred` and keeps it open.

Relabeling an issue does not remove its code from an existing candidate.
Excluding included code needs a reviewed revision and a new candidate
validation. Material new scope or risk, unplanned destructive actions and new
policy exceptions go back to the operator. Scope approval does not waive review,
tests or evidence.
