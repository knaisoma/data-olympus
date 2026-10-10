# STD-U-821 amendment 1.5

Source: `knaisoma/company-knowledge`,
`universal/foundation/STD-U-821-release-branch-and-versioning.md` at commit
`2441459`, section "Amendment 1.5". The section below is copied verbatim.

## Amendment 1.5: release tooling repairs on main after a release

Status: RATIFIED on 2026-10-10 by the program lead under the operator's standing
authorization of 2026-10-04 (run the program to completion; decisions are
recorded, owner-only steps reported). The operator was informed of the decision
and the amendment text in the program chat on 2026-10-10 and did not object.
The rules below are in force. Revocation by the operator removes this line and
makes tooling refuse the record again.

Ratification: 2026-10-10

### Why

The branch contract says `main` advances only through releases and that the
stable base is the unique stable tag on `B`. The release and promotion
workflows run from the definition on `main` (workflow-run and dispatch events
use `main`'s copy), so a defect that only a real release can reveal can be
repaired only on `main`, after the stable tag exists. The first release of
data-olympus (0.11.1) needed four such commits after its tag (a verifier fix,
a resume-after-tag fix and two documentation commits). Afterwards `main` is
ahead of the last stable tag, so a cut at its head carries no stable tag, and a
cut at the tag does not contain `main` (the engine refuses it as a recut).

### Repairs to the release tooling on main after a release

- **Scope.** After a stable release of the new model, `main` MAY receive
  commits outside a release only when they repair the release tooling: the
  workflows under `.github/workflows/`, the scripts they call, the release
  documentation and rules, and the tests of those files. A change to product
  behaviour MUST NOT use this route. A repair that does not have to be defined
  on `main` MUST land through `release/new`.
- **Review.** Each repair is a reviewed pull request squash merged to `main`
  under the review tier of the file it changes (a privileged publication or
  promotion workflow needs the STD-U-602 verdict). Merging it is recorded in
  the pull request.
- **Release content.** The repairs count in the impact and the release notes of
  the next release, like every non-merge commit in `v<base>..H`.
- **The next cut.** The cut uses the record route of amendment 1.3 with this
  difference only: the record's `base` is the version of the highest stable tag
  of the product. `v<base>` MUST be an ancestor of `B` and a strict ancestor of
  the anchor, so the route cannot be used when `main` is not ahead of the tag.
  Every other rule of amendment 1.3 stays: the record commit is a single
  non-merge commit that adds only `release/ADOPTION.json`, the anchor is its
  sole parent, the record is absent at the anchor, `B` carries no stable tag,
  the record is read only from the tree of `B`, the record is single use and
  is deleted by the first commit on `release/new` after the cut, and no hotfix
  is cut during that cycle.
- **Pinned base.** The base MUST be recorded in the product's adoption issue
  for that use (one issue or one issue update per use, with the anchor, the
  base and the repairs that moved `main`).
- **Single use per record.** A record is valid only while its base is the
  highest stable tag. After the next stable release it is invalid, and a later
  need requires a new record under this amendment.
- **Malformed release history.** Tooling MUST refuse the record when any tag
  that starts with `v` followed by a digit is neither a strict stable version
  nor a strict prerelease, so a malformed tag cannot hide a higher release.
- **Ratification.** Tooling MUST refuse the record unless the standard carries
  both the exact line `Ratification: <date>` of amendment 1.3 and the exact line
  `Ratification: <date>` of this amendment, each with a valid past or present
  date, evaluated against the vendored copies at the reviewed revision.

Rationale: the record route already exists for a released product with `main`
ahead of its last stable tag. Reusing it keeps one mechanism, but the
single-use and pinned-base rules of 1.3 were written for the adoption case, so
this amendment states the reuse explicitly, limits what may land on `main`
outside a release, and keeps a ratification line of its own so that every
off-model cut traces to an operator decision.
