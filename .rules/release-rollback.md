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
1. Publish the complete candidate from an exact source SHA. The candidate
   transaction includes its wheel, sdist, `release-provenance.json`, PyPI
   prerelease, GHCR image, and GitHub prerelease.
2. Canary: `set-channel source=X.Y.Z-rc.N` makes Keel roll the candidate onto
   kn-dev.
3. Pre-release verify: `data-olympus verify --target <kn-dev ingress>` must be
   green, including default tool discovery and enforcement checks.
4. Obtain the Paperclip approval to ship from the operator.
5. Merge the exact reviewed candidate source to `main` with the required merge
   method. Wait for CI on the resulting `main` SHA.
6. Explicitly dispatch `tag-release.yml` through `workflow_dispatch`, passing
   `candidate_tag=X.Y.Z-rc.N`. The workflow accepts only the highest complete
   candidate, enters the protected `pypi` environment, compares the stable
   Python payload with the candidate payload, publishes stable PyPI, creates
   `vX.Y.Z` at the candidate source SHA, promotes the exact GHCR digest, and
   creates the GitHub release.
7. Promote the canary channel to stable with `set-channel source=vX.Y.Z`.
8. Run post-release verification against kn-dev.

## Rollback

* Candidate publication interrupted before finalization: rerun the same candidate
  number only from the same exact source SHA. Existing PyPI files and the GHCR
  image must match their recorded hashes and revision. Otherwise stop and use a
  higher candidate number.
* Canary failure or rejected approval: restore `kndev` to the recorded rollback
  point. The candidate remains externally visible as a prerelease. A published
  PyPI candidate cannot be replaced. Yank it when it is unsuitable for further
  testing, then publish a corrected higher candidate number.
* Post-release failure: restore `kndev` to the rollback point, yank the stable
  PyPI version, mark the GitHub release as a prerelease, open a `release blocked`
  issue, and notify the operator. Published files and version tags remain
  immutable.

## Note

Stable image promotion is byte identical. `tag-release.yml` re-tags the verified
`X.Y.Z-rc.N` digest to `vX.Y.Z`, `stable`, and `latest`; it has no image build
job. Stable Python artifacts are rebuilt from the same exact source SHA and
compared with the candidate wheel. Only normalized version metadata may differ.
