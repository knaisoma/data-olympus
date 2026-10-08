# STD-U-821 amendment 1.3

Source: `knaisoma/company-knowledge`,
`universal/foundation/STD-U-821-release-branch-and-versioning.md` at commit
`785bb77`, section "Amendment 1.3". The section below is copied verbatim.

## Amendment 1.3: adoption cut and public-product preview

Status: RATIFIED by the operator on 2026-10-07. The rules below are in force
from the ratification date. Provenance: the operator's own message in the
controller chat session on 2026-10-07, after a walkthrough of this amendment,
reads verbatim: "for the other it's fine, let's use today as confirmation date
and continue" ("the other" being this amendment, and today being 2026-10-07).
The controller session recorded it here; the operator may overrule it by a new
reviewed change.

Ratification: 2026-10-07

### Adoption cut for an already released product

This amendment amends two rules for one case: "the stable base MUST be the
unique stable tag on `B`" and "`main` MUST advance only through releases". The
bootstrap rule covers a product with no stable tag. A product that has already
released can reach adoption with `main` ahead of its last stable tag, because
commits landed under the previous process. The cut commit `B` then carries no
stable tag. For that one case, a product MAY record an adoption anchor instead
of a tag on `B`:

- The record is the file `release/ADOPTION.json`, with the fields `anchor` (a
  full commit SHA) and `base` (the last stable version, pinned to the value
  recorded in the product's adoption issue).
- The record commit is the one permitted advance of `main` outside a release.
  It MUST be a single non-merge commit, reached through a reviewed PR, whose
  only change is adding the record, and it is the cut: `B` is that commit.
- The record is valid only when all of these hold: `B` has `anchor` as its sole
  parent; the record is absent at `anchor`; `B` carries no stable tag; and
  `v<base>` is the highest stable tag of the product by SemVer precedence and
  an ancestor of `B`.
- The record is read only from the tree of `B`, never from `H`.
- The stable base for the first cycle is `v<base>`. Count `N` is computed from
  `B..H` as usual. Impact and the release notes are computed over every
  non-merge commit in `v<base>..H`, so commits that landed between the tag and
  the anchor are neither under-counted nor omitted from the notes.
- The record is single use. Once any stable tag newer than `v<base>` exists,
  `v<base>` is no longer the highest stable tag and the record is invalid; once
  `B` carries a stable tag it is ignored. The first release MUST delete the file
  in a commit on `release/new`, and promotion MUST fail while the file exists in
  the reviewed tree `H`.
- No hotfix MAY be cut from `hotfix/new` during the adoption cycle, because the
  squash of a hotfix could not have sole parent `B`. A repair needed before the
  first release uses the first release itself.
- Tooling MUST refuse the record unless the standard carries the exact line
  `Ratification: <date>` with a valid past or present date (this amendment was
  ratified on 2026-10-07). The record is evaluated against the file at the
  reviewed revision, and a missing or malformed ratification line makes the
  result unratified and not promotable.
- The record MUST be noted in the product's adoption issue with the anchor,
  the base and the reason `main` was ahead of the tag.

Rationale: a record cannot name the commit that contains it, so the record
names its parent. Requiring the sole-parent relation and a record absent at the
anchor prevents an anchor from pointing at a distant ancestor whose tree merely
matches, and the single-use rules stop the record from becoming a standing way
around the stable-base rule.

An operator MAY instead release the commits on `main` under the previous
process and cut at the resulting tag, which needs no record.

### Preview for a public library and service product

The internal-tool preview exception does not cover a product published to
public registries. For data-olympus (public Python package, container image and
MCP registry entry, no per-pull-request deployment) the operator approved this
scoped exception on 2026-10-07. It covers the preview wording in the Branch
contract, the hotfix contract and the mandatory `CONTRIBUTING.md` paragraph for
this product only:

- Owner: operator.
- Scope: the data-olympus repository only. No other product.
- Reason: the product has no deployable per-pull-request environment; its
  deployment is a cluster workload that follows reviewed digests only.
- Substitute for a preview: every pull request runs the built OCI image,
  checks `/api/v1/health` and an MCP handshake, and publishes the wheel, source
  distribution and an OCI archive as downloadable workflow artifacts. The image
  check is a required status check.
- Compensating controls: every push to `release/new` builds its computed
  candidate; staging validation and all STD-U-822 gates apply before promotion;
  code merged to `release/new` never runs with publication credentials,
  because publication is a separate workflow defined on `main`.
- Evidence: the product records this exception and the required check names in
  `.rules/exceptions.md`.
- Recheck date: 2027-04-01. No automatic extension.

