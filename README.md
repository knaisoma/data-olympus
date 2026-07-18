# data-olympus

[![knaisoma/data-olympus MCP server](https://glama.ai/mcp/servers/knaisoma/data-olympus/badges/score.svg)](https://glama.ai/mcp/servers/knaisoma/data-olympus)

**New here? Start with [WHY.md](WHY.md).** It is the story behind the project: the
problem we kept hitting with coding agents, what data-olympus does differently, how
it relates to Google's Open Knowledge Format, and where our benchmarks say it is
strong and where it is not. The rest of this README is the technical reference.

data-olympus is a governance-grade knowledge-base format and server for agent workforces. It is readable by [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) (OKF) consumers: it inherits OKF's directory structure, frontmatter conventions, reserved filenames, and link model, then layers governance extensions on top (stable `id`, controlled `type`/`status`/`tier` fields, `supersedes` chains) plus a single-writer MCP server and a CLI. CI proves two concrete directions against official Google OKF commit `d44368c15e38e7c92481c5992e4f9b5b421a801d`: its reference visualization consumer reads every concept in `example-bundle`, and data-olympus imports, lints, indexes, searches, and retrieves the pinned official Bitcoin sample. This is fixture-scoped interoperability evidence, not a blanket guarantee for every OKF bundle or future upstream revision. The result is a git-native, version-controlled document graph of engineering standards, architectural decisions, and project knowledge that agents and humans can read, search, and extend without any proprietary service.

It governs *decisions*, not code. When an agent is about to make a choice (a library, a pattern, a migration), data-olympus surfaces the established standard or decision that should govern that choice. It is deliberately **not** a code-search, reference-finding, or "where is X used" tool: LSP, grep, and Sourcegraph already do that well. The retrieval task it targets is coding-intent to governing-rule, and it helps where current model interaction during vibe-coding is weakest: keeping the model aligned to patterns the team has already established as correct.

**Status: pre-1.0 beta. Stable releases are distributed through PyPI and GHCR.**

## Why

- **Portable, no lock-in.** The entire KB is a directory of markdown files in git. No database, no proprietary schema, no vendor.
- **Git-native diffs and review.** Every change is a commit. Proposed edits go through a pending queue before commit; history is plain git log.
- **Agent and human readable.** Plain markdown with YAML frontmatter. No SDK required to read or author a document.
- **Governed multi-agent writes.** The single-writer MCP pipeline (advisory locks, per-session worktrees, durable push queue) prevents concurrent write races without requiring distributed locking infrastructure.
- **Queryable by status, tier, and type.** Filter by `status: accepted`, `tier: T1`, or `type: decision` without post-processing. The `supersedes` chain makes it possible to trace decision history across the graph.
- **Tested with official OKF tooling.** CI pins an exact Google OKF revision and proves both consumption directions over committed fixtures. The pin, fixture checksum, and Apache 2.0 license provenance live in `tests/okf/reference.json`.

## Quickstart

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/). Run the stable CLI
directly from PyPI:

```bash
uvx --from data-olympus data-olympus --help
```

Install it persistently when you are ready to create a bundle and run the
server:

```bash
uv tool install data-olympus
data-olympus init my-kb
data-olympus-mcp --help
```

An announced candidate remains opt in through its exact PyPI version:

```bash
uvx --from 'data-olympus==0.6.0rc3' data-olympus --help
```

See `docs/quickstart.md` for bundle initialization, server startup, readiness,
agent registration, and the contributor source installation.

See `docs/adoption.md` for the full bundle authoring guide.

## Documentation

- [`SPEC.md`](SPEC.md): format specification (bundle layout, frontmatter schema, serving contracts).
- [`docs/quickstart.md`](docs/quickstart.md): verified local-run procedure.
- [`docs/adoption.md`](docs/adoption.md): bring-your-own-KB guide (author, lint, index, serve, wire an agent).
- [`docs/serving.md`](docs/serving.md): single-replica serving model, read-only replicas, git pull loop, health/readiness/liveness split, proxy headers, audit-log rotation.
- [`docs/operations.md`](docs/operations.md): production runbook — backup, upgrade, recovery playbooks (degraded/fetch-failed, history rewrite, frozen/demoted push entries, orphaned locks), and the health/alerting model.
- [`docs/comparison.md`](docs/comparison.md): how data-olympus relates to OKF, enterprise catalogs, markdown KB tools, agent-context conventions, RAG, and ADR tooling.
- [`docs/okf-profile.md`](docs/okf-profile.md): field-by-field OKF profile — which governance extensions are stable, which are runtime-only serving fields, and which are experimental candidates.
- [`docs/glama.md`](docs/glama.md): Glama registry claim, release, and score-maintenance notes.
- [`docs/enforcement.md`](docs/enforcement.md): turning the KB into a mandatory consultation gate (hooks, `kb enforce`).
- [`benchmarks/README.md`](benchmarks/README.md): retrieval benchmark methodology and how to reproduce the numbers in `docs/comparison.md`.
- [`SECURITY.md`](SECURITY.md): supported versions and how to report a vulnerability.

## License

Apache 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
