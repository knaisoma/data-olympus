# Quickstart: Run data-olympus locally

This guide shows how to install, start, and query the data-olympus MCP server
against the included example bundle. Every command below was verified on macOS
with Python 3.13 and uv.

## 1. Install

### Install from PyPI (recommended)

Run the CLI directly with no clone, using `uvx`:

```bash
uvx data-olympus --help
```

Or install it as a persistent tool:

```bash
uv tool install data-olympus
data-olympus --help
```

Then run the guided setup wizard to probe your server endpoint, wire your
coding agents (Claude Code, Codex, Gemini, OpenCode), and optionally install the
enforcement hooks:

```bash
data-olympus setup          # interactive
data-olympus setup --check  # read-only doctor summary (also the update check)
```

> The PyPI package is published by the release chain via Trusted Publishing.
> Until the one-time pypi.org publisher is configured (see
> `docs/releases/pypi-trusted-publishing.md`), install from source with the dev
> path below.

### Install from source (development)

```bash
# From the repo root
uv venv && uv pip install -e '.[dev]'
```

Or simply use `uv run` (which handles the venv automatically):

```bash
uv run data-olympus-mcp --help   # confirms the entry point is installed
```

## 2. Run the server

```bash
./scripts/run-local.sh
```

The script:

- Copies `example-bundle/` to `/tmp/data-olympus-demo-kb`
- Git-initializes the copy (the server requires a git repo)
- Starts the MCP server at `http://localhost:8080`

The server logs startup to stdout. Wait for a line like:

```
INFO     data_olympus  starting streamable HTTP MCP on port 8080
```

## 3. Query with curl

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

## 4. Lifecycle-aware retrieval: in-force vs superseded

The example bundle ships a real supersession pair in
`universal/foundation/`: `STD-U-003` (`status: superseded`,
`superseded_by: STD-U-004`) and `STD-U-004` (`status: active`,
`supersedes: STD-U-003`), the standard that replaced it. This section shows
the two ways `kb_search` treats that pair differently.

**Default search** applies a soft status-aware rerank: an `active` document is
promoted ahead of the `superseded` document it replaced, but the superseded
document is still returned (useful when an agent or human is tracing decision
history).

```bash
curl -fsS "http://localhost:8080/api/v1/search?q=commit%20format&limit=5"
```

The top two hits are `STD-U-004` (currently in force) ranked ahead of
`STD-U-003` (`status: superseded`) — both present, active first. In the compact
default the in-force hit carries no `status` field while the superseded hit shows
`"status": "superseded"` (the deviation an agent must act on); add `verbose=true`
to see `"status": "active"` spelled out on every hit.

**`in_force=true`** is a hard filter, not a rerank: it excludes every
not-currently-governing status (`superseded`, `deprecated`, `draft`,
`proposed`, `rejected`) from the result set entirely, before ranking.

```bash
curl -fsS "http://localhost:8080/api/v1/search?q=commit%20format&limit=5&in_force=true"
```

`STD-U-003` does not appear in the response at all: an agent that only wants
guidance currently in force (for example, before writing a commit message)
gets a result set that can never surface retired governance, rather than
relying on the rerank to have pushed it down far enough.

## 5. Query with the kb CLI

```bash
KB_ENDPOINT=http://localhost:8080 ./bin/kb health -o plain
KB_ENDPOINT=http://localhost:8080 ./bin/kb search writing -o plain
KB_ENDPOINT=http://localhost:8080 ./bin/kb outline -o plain
```

Set `KB_ENDPOINT` in your shell to avoid repeating it:

```bash
export KB_ENDPOINT=http://localhost:8080
./bin/kb search writing
```

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

## 7. Use your own bundle

Point the server at any git-initialized directory following the
`example-bundle/` layout:

```bash
KB_MAIN_PATH=/path/to/your-bundle \
  KB_INDEX_PATH=/tmp/your-kb.db \
  KB_REMOTE_URL="" \
  uv run data-olympus-mcp
```

See `SPEC.md` for the full document schema and tier conventions.
