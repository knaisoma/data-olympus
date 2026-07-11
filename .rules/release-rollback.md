# Rule: release promotion and rollback (kn-dev canary)

Status: active
Since: 2026-07-11
Applies to: the data-olympus release train (staged-promotion pipeline)

## Channel model

kn-dev runs whatever the moving ghcr tag `:kndev` points at (Keel `policy: force`,
`match-tag: true`, poll). The pipeline moves `:kndev` via the `set-channel.yml`
workflow (`gh workflow run set-channel.yml -f source=<tag>`); it never rebuilds an
image, so the channel and its source share one digest.

## Promotion sequence (cutter, after approval)

0. Record the rollback point: note the tag `:kndev` currently points at (the stable
   version kn-dev is running, e.g. the latest non-prerelease GitHub Release, or read
   it with `docker buildx imagetools inspect ghcr.io/knaisoma/data-olympus:kndev`).
   The canary and post-release rollbacks below re-point `:kndev` back at this value.
1. Canary: `set-channel source=vX.Y.Z-rc.N` -> Keel rolls the RC onto kn-dev.
2. Pre-release verify: `data-olympus verify --target <kn-dev ingress>` must be green.
3. Paperclip approval-to-ship (operator).
4. Merge the release PR -> `tag-release.yml` cuts tag vX.Y.Z, builds the stable
   image, publishes PyPI, creates the GitHub Release.
5. Promote the canary to stable: `set-channel source=vX.Y.Z` -> kn-dev runs stable.
6. Post-release verify against kn-dev.

## Rollback

- Canary failure (pre-release verify red, or approval rejected): nothing external
  shipped. `set-channel source=<the rollback-point tag from step 0>` -> Keel restores
  the prior version. Roll a new `-rc.(N+1)` forward if fixable, else block.
- Post-release failure (stable already shipped): `set-channel source=<the rollback-point tag from step 0>` to restore kn-dev; **yank** the just-published version on PyPI (PyPI has no CLI yank: do it in the PyPI web UI, Project -> the release -> Options -> Yank; yank hides it from resolvers, it cannot be deleted or replaced); mark the GitHub Release as a draft/prerelease; open a
  `release blocked: post-release verify failed` issue and notify the operator.

## Note

Stable image promotion is byte-identical: `tag-release.yml` re-tags the verified
`X.Y.Z-rc.N` digest to `vX.Y.Z` (it builds fresh only as a manual-tag fallback when
no RC exists). The PyPI wheel is built fresh from the tagged commit (the RC is
image-only). The promoted image carries the RC build's OCI labels
(`org.opencontainers.image.version` reads the rc tag); this is a cosmetic
provenance detail, not a content difference.
