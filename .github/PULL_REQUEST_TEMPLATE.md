## Summary

<!-- One or two sentences describing what this PR does and why. -->
<!-- External contributions: branch from and target release/new, never main.
Use a Conventional Commit title that preserves the highest source-commit
impact and breaking details. Do not commit a target package version.
CI runs tests and wheel/sdist smoke checks per PR; no hosted preview or per-PR image.
The RC wheel and image will come from rc-build.yml once enabled;
that pipeline is delivered separately, not yet in the repository.
Maintainer release batches target main with release: X.Y.Z and generated notes. -->

## Type of change

<!-- Check all that apply. -->

- [ ] Tool / code change (CLI, MCP server, `kb` client, deploy)
- [ ] Spec / format change (requires a linked Spec Proposal issue with maintainer sign-off)
- [ ] Documentation
- [ ] CI

<!-- If this is a spec change, link the Spec Proposal issue here: -->
<!-- Spec Proposal issue: # -->

## Checklist

- [ ] Base is `release/new` for an external contribution
- [ ] Source commits and proposed squash message use Conventional Commits without lowering impact
- [ ] Functional changes have an entry under `CHANGELOG.md` `[Unreleased]` (or this is nonfunctional)
- [ ] Tests pass (`uv run pytest`)
- [ ] `ruff` reports no errors (`uv run ruff check .`)
- [ ] `data-olympus lint` exits 0 on any bundle touched by this PR
- [ ] Documentation updated (if the changed behaviour is documented)
- [ ] `SPEC.md` and the validator are consistent (required if the schema changed)
- [ ] No em-dashes in any prose added or modified by this PR
- [ ] No credentials or secrets committed
