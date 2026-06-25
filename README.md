# data-olympus

**New here? Start with [WHY.md](WHY.md).** It is the story behind the project: the
problem we kept hitting with coding agents, what data-olympus does differently, how
it relates to Google's Open Knowledge Format, and where our benchmarks say it is
strong and where it is not. The rest of this README is the technical reference.

data-olympus is a governance-grade knowledge-base format and server for agent workforces. It is an OKF-compatible profile (a conformant extension of the [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)) with governance extensions (stable `id`, controlled `type`/`status`/`tier` fields, `supersedes` chains) plus a single-writer MCP server and a CLI. The result is a git-native, version-controlled document graph of engineering standards, architectural decisions, and project knowledge that agents and humans can read, search, and extend without any proprietary service.

It governs *decisions*, not code. When an agent is about to make a choice (a library, a pattern, a migration), data-olympus surfaces the established standard or decision that should govern that choice. It is deliberately **not** a code-search, reference-finding, or "where is X used" tool: LSP, grep, and Sourcegraph already do that well. The retrieval task it targets is coding-intent to governing-rule, and it helps where current model interaction during vibe-coding is weakest: keeping the model aligned to patterns the team has already established as correct.

**Status: pre-release (v0.1).**

## Why

- **Portable, no lock-in.** The entire KB is a directory of markdown files in git. No database, no proprietary schema, no vendor.
- **Git-native diffs and review.** Every change is a commit. Proposed edits go through a pending queue before commit; history is plain git log.
- **Agent and human readable.** Plain markdown with YAML frontmatter. No SDK required to read or author a document.
- **Governed multi-agent writes.** The single-writer MCP pipeline (advisory locks, per-session worktrees, durable push queue) prevents concurrent write races without requiring distributed locking infrastructure.
- **Queryable by status, tier, and type.** Filter by `status: accepted`, `tier: T1`, or `type: decision` without post-processing. The `supersedes` chain makes it possible to trace decision history across the graph.
- **OKF-compatible.** Any OKF consumer can read a data-olympus bundle. Any OKF-produced bundle can be governed by data-olympus tools.

## Quickstart

```bash
# Install
uv venv && uv pip install -e '.[dev]'

# Lint the example bundle
uv run data-olympus lint example-bundle

# Start the MCP server against the example bundle
./scripts/run-local.sh
```

See `docs/quickstart.md` for the full local-run walkthrough, including curl and `kb` CLI queries.

## Documentation

- [`SPEC.md`](SPEC.md): format specification (bundle layout, frontmatter schema, serving contracts).
- [`docs/quickstart.md`](docs/quickstart.md): verified local-run procedure.
- [`docs/adoption.md`](docs/adoption.md): bring-your-own-KB guide (author, lint, index, serve, wire an agent).
- [`docs/serving.md`](docs/serving.md): single-replica serving model, read-only replicas, git pull loop.
- [`docs/comparison.md`](docs/comparison.md): how data-olympus relates to OKF, enterprise catalogs, markdown KB tools, agent-context conventions, RAG, and ADR tooling.

## License

Apache 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
