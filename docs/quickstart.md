# Quickstart: Install and run data-olympus

This guide installs Data Olympus from PyPI, starts the MCP server against a
local bundle, checks readiness, and connects coding agents. Python 3.13 and
[`uv`](https://docs.astral.sh/uv/) are required.

## 1. Install

Run the current stable CLI without cloning the repository:

```bash
uvx --from data-olympus data-olympus --help
```

For repeated use, install both console commands as a persistent tool:

```bash
uv tool install data-olympus
data-olympus --help
data-olympus-mcp --help
```

To test an announced release candidate without replacing the persistent stable
tool, select its exact PEP 440 version:

```bash
uvx --from 'data-olympus==0.6.0rc3' data-olympus --help
```

## 2. Create a bundle and run the server

Create a valid starter bundle and initialize its git history:

```bash
data-olympus init my-kb
git -C my-kb init -b main
git -C my-kb config user.name "Local Operator"
git -C my-kb config user.email "operator@localhost"
git -C my-kb add .
git -C my-kb commit -m "docs: initialize knowledge bundle"
```

Start the installed MCP server. Runtime state stays outside the bundle:

```bash
mkdir -p .data-olympus
KB_MAIN_PATH="$PWD/my-kb" \
KB_INDEX_PATH="$PWD/.data-olympus/kb.db" \
KB_REMOTE_URL="" \
KB_WORKTREE_ROOT="$PWD/.data-olympus/worktrees" \
KB_PENDING_ROOT="$PWD/.data-olympus/pending" \
KB_PUSH_QUEUE_ROOT="$PWD/.data-olympus/push-queue" \
KB_AUDIT_LOG_PATH="$PWD/.data-olympus/audit.log" \
data-olympus-mcp
```

Wait for readiness in another terminal:

```bash
curl -fsS http://localhost:8080/readyz
curl -fsS http://localhost:8080/api/v1/health
```

## 3. Connect coding agents

Run the guided setup to register the MCP endpoint with detected Claude Code,
Codex, Gemini, and OpenCode installations. The doctor command is read only.

```bash
data-olympus setup --endpoint http://localhost:8080
data-olympus setup --check --endpoint http://localhost:8080
```

## 4. Query with curl

```bash
# Health
curl -fsS http://localhost:8080/api/v1/health

# Search
curl -fsS "http://localhost:8080/api/v1/search?q=writing"

# Outline
curl -fsS http://localhost:8080/api/v1/outline
```

### Compact responses by default

The read tools (`kb_search`, `kb_get`, `kb_list`, `kb_outline`, `kb_health`)
return a **token-compact** shape by default, because their consumer is almost
always an LLM paying for every token in its context. Compared with the full
representation, the compact default:

- `kb_search`: each hit is `{id, title, snippet}` plus `status` **only when a hit
  is not currently in force** (e.g. `superseded`) and `type` when set. The `query`
  echo, the per-hit `path`, and the raw relevance `score` are dropped. To read a
  hit in full, call `kb_get` with its `id`; array order conveys rank.
- `kb_get`: keeps the full `content_markdown` body (that is why you call it) plus
  `source_commit`/`last_modified` provenance, and trims low-value envelope fields
  (`path`, `git_remote_url`, `last_modified_source`). `path` is recoverable with
  `kb_get(id, verbose=true)`.
- `kb_list`: drops the per-entry `path` (fetch via `kb_get(id)`).
- `kb_health`: keeps the core snapshot and omits diagnostic fields that are unset.
- `kb_outline`: already lean; unchanged.

Pass `verbose=true` (REST query param, or the `verbose` MCP tool argument) to get
the full legacy shape with every field:

```bash
# Full/legacy shape (query, path, score, and full health envelope restored)
curl -fsS "http://localhost:8080/api/v1/search?q=writing&verbose=true"
```

The `kb` CLI always requests `verbose=true` so its plain-text output keeps
showing paths.

## 5. Lifecycle-aware retrieval: in-force vs superseded

The generated starter bundle ships a real supersession pair in
`universal/foundation/`: `STD-INIT-001` (`status: superseded`,
`superseded_by: STD-INIT-002`) and `STD-INIT-002` (`status: active`,
`supersedes: STD-INIT-001`), the standard that replaced it. This section shows
the two ways `kb_search` treats that pair differently.

**Default search** applies a soft status-aware rerank: an `active` document is
promoted ahead of the `superseded` document it replaced, but the superseded
document is still returned (useful when an agent or human is tracing decision
history).

```bash
curl -fsS "http://localhost:8080/api/v1/search?q=example%20standard&limit=5"
```

The top two hits are `STD-INIT-002` (currently in force) ranked ahead of
`STD-INIT-001` (`status: superseded`) — both present, active first. In the compact
default the in-force hit carries no `status` field while the superseded hit shows
`"status": "superseded"` (the deviation an agent must act on); add `verbose=true`
to see `"status": "active"` spelled out on every hit.

**`in_force=true`** is a hard filter, not a rerank: it excludes every
not-currently-governing status (`superseded`, `deprecated`, `draft`,
`proposed`, `rejected`) from the result set entirely, before ranking — and,
for docs carrying a `validity` frontmatter block, any doc outside its
validity window (past `valid_until`, or before a future `valid_from`).

```bash
curl -fsS "http://localhost:8080/api/v1/search?q=example%20standard&limit=5&in_force=true"
```

`STD-INIT-001` does not appear in the response at all: an agent that only wants
guidance currently in force (for example, before writing a commit message)
gets a result set that can never surface retired governance, rather than
relying on the rerank to have pushed it down far enough.

Note that a doc past its `validity.valid_until` is excluded from DEFAULT
search results too, not just from `in_force=true` queries; pass
`include_expired=true` to see it, or fetch it directly with `kb_get` (which
always resolves by id). See [`docs/serving.md`](serving.md) and SPEC.md
section 4.2 for the full validity semantics.

## 6. Docker path

Build and start with Docker Compose:

```bash
docker compose -f deploy/docker/compose.yaml up --build
```

By default the container serves an empty volume. Bind-mount a git-initialized
bundle to `KB_MAIN_PATH` to serve real content:

```bash
docker compose -f deploy/docker/compose.yaml run \
  -e KB_MAIN_PATH=/my-kb \
  -v /path/to/my-kb:/my-kb:ro \
  data-olympus-mcp
```

The bundle at `/path/to/my-kb` must be a git repository (run `git init` inside
it first).

## 7. Serve an existing bundle

Point the server at any git-initialized directory following the
`example-bundle/` layout:

```bash
KB_MAIN_PATH=/path/to/your-bundle \
  KB_INDEX_PATH=/tmp/your-kb.db \
  KB_REMOTE_URL="" \
  uv run data-olympus-mcp
```

See `SPEC.md` for the full document schema and tier conventions.

## 8. Contributor installation

Editable installation is for repository development, not normal onboarding:

```bash
git clone https://github.com/knaisoma/data-olympus.git
cd data-olympus
uv sync --all-groups
uv run pytest -q
```

Use `uv run data-olympus` and `uv run data-olympus-mcp` while developing.
The repository also provides the lower level `./bin/kb` REST helper for
maintainer diagnostics.
