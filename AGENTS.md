# Data Olympus

## Project Overview

Data Olympus is the knowledge base service that coding agents consult before making decisions. It indexes the `company-knowledge` corpus and serves it over MCP and REST, and it enforces that agents consult it before governed changes.

It is the authority behind the tier model (T1 Universal, T2 Stack, T3 Project, T4 Component; see GDEC-017), decisions, and workflows.

Stack, as declared in `pyproject.toml`:

- Python `>=3.13`, managed with `uv`
- Runtime dependencies are deliberately few: `fastmcp`, `pydantic`, `pyyaml`, `regex`
- Two entry points: `data-olympus-mcp` (the MCP server) and `data-olympus` (the CLI)

There is no web framework and no database server. Keep it that way unless a decision says otherwise: the small dependency surface is a feature, because this service is a dependency of every other project's agent workflow.

## Architecture

**Storage** is SQLite. The index lives at `KB_INDEX_PATH` (default `/index/kb.db`) and is rebuilt atomically into a temporary file before being swapped in.

**Retrieval** is a SQLite FTS5 index with BM25 ranking, layered with progressively broader fallbacks:

| Module | Role |
|---|---|
| `index.py` | FTS5 index over the KB markdown corpus |
| `query_expansion.py` | synonym and acronym expansion |
| `cooccurrence.py` | corpus co-occurrence expansion, embedding free |
| `trigram.py` | fuzzy fallback for typos and partial identifiers |
| `embeddings.py` | **optional** local ONNX hybrid ranking, blended with BM25 |

`embeddings.py` is **off by default**. It requires the `embeddings` extra and `KB_EMBEDDINGS_MODE`, imports its libraries lazily, and never calls an external API at query time. With the extra absent and the variable unset, the product is byte-for-byte unchanged. Do not describe this service as a vector or RAG pipeline: the default retrieval path is lexical.

**Serving** is `server.py` (MCP tools) and `rest_api.py` (the REST mirror). The enforcement surface is `enforce_policy.py`, `write_gate.py`, `governed_lane.py`, `auth.py`, `principals.py`, and `rate_limit.py`.

**Git backing**: the corpus is a git repository. `git_ops.py`, `push_queue.py`, and `pending.py` handle refresh and the proposed-write pipeline.

**Deployment** is `deploy/docker` and `deploy/k8s`. On kn-dev the workload is Keel managed and auto-upgrades on a new minor or patch tag, so a published release reaches the cluster without a manual apply. See `operator/laptop.md` in company-knowledge before bumping tags or attempting a rollback.

## Development Workflow

```bash
uv sync                          # install
uv run data-olympus --help       # CLI
uv run data-olympus-mcp          # MCP server
```

Branching and review follow the operator rules: work in a dedicated worktree under `.worktrees/<branch>` (STD-U-505), never commit directly to `main`, and never bypass hooks with `--no-verify` (STD-U-508). Squash and merge commits are both enabled; rebase merging is not.

Pull request titles are linted as Conventional Commits by `.github/workflows/pr-title-lint.yml`.

**This repository dogfoods its own enforcement.** Governed changes are blocked until an explicit `kb_consult` is recorded, and a consultation expires after 300 seconds, so re-consult if time passes before the gated action. The pre-commit gate runs `data-olympus report --staged` and keys on the working directory's basename, which in a worktree is the worktree name, not the repository name.

## Code Standards

Universal standards live in company-knowledge and are referenced, not copied: STD-U-001 (no sugarcoating), STD-U-002 (writing style, no em dashes), STD-U-003 (code review honesty).

Repository specific rules live in [.rules/](./.rules/) and are authoritative for the topics they cover:

- `release-routine.md`, `release-planning.md`, `release-rollback.md`
- `versioning.md`, `changelog-per-release.md`

Do not restate those rules here. This file points at them.

Never add agent credit attribution to commits, pull request bodies, or any platform field (STD-U-510).

## Testing Requirements

**Set the environment up the way CI does, or two tests fail for reasons that have nothing to do with your change:**

```bash
uv venv --python 3.13
uv pip install -e '.[dev]'
```

`uv run pytest` on its own is not equivalent. `tests/test_server_cli.py` and `tests/test_smoke.py` spawn a subprocess with `sys.executable` and read installed distribution metadata, so without the editable install they fail with `No module named data_olympus.server` and a stale version assertion. CI is green while the same command fails locally, which makes this look like a real regression when it is not. Anything that gates on `uv run pytest` without the editable install (a release routine, a scheduled job) will block on a false failure.

Then, and a change is not ready until all of these pass:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -v
bats -r tests                                   # bin/kb CLI contract tests
uv run python scripts/okf_conformance.py verify-pin
uv run python scripts/check_benchmark_docs.py
uv run data-olympus lint example-bundle         # dogfood conformance
```

Run the linter before the tests. It runs first in CI, so a lint failure hides every test result behind it.

## Deployment

Releases are cut from `main` by the release routine documented in `.rules/release-routine.md`, which is the authority. In outline: `scripts/compute_release.py` decides releasability, CI tags on merge, and publication flows to PyPI and GHCR.

Pre-1.0 version mapping is implemented in `compute_release.py` and documented in `.rules/versioning.md`. Trust the script's `next_version` rather than inferring a bump by hand.

## Key Rules

1. **The KB is authoritative.** Consult it before architectural decisions, new features, API design, or refactoring. If a standard exists, follow and cite it. If one is missing, propose it.
2. **Tier precedence** is T1 > T2 > T3 > T4, most specific wins within a tier (GDEC-017).
3. **Index freshness is not automatic.** A corpus change is not visible until the index rebuilds. `kb_health` reports `kb_commit`, `index_built_at`, `staleness_seconds`, `degraded`, and `pending_count`.
4. **Secrets never enter the KB.** They are prompted or fetched from the secrets manager, never written to a file, a commit message, or a pull request body (STD-U-005).
5. **Public-facing docs only.** `docs/` is for users. Internal plans, specs, and scratch are gitignored and must not be committed here.
6. Read and follow all rules in [.rules/](./.rules/).
