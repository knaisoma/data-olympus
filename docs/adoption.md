# Adoption guide: bring your own KB

This guide explains how to create, validate, and serve a data-olympus knowledge
bundle from your own content. It covers the full path from a fresh directory to
a running MCP server your agents can query.

## 1. Install

From the repo root:

```bash
uv venv && uv pip install -e '.[dev]'
```

Or skip the venv step and use `uv run` to drive the CLI directly. Every
command in this guide works with either approach; the examples below use
`uv run`.

## 2. Author a bundle

A bundle is a directory of markdown files with YAML frontmatter. The layout
mirrors `example-bundle/` in this repo.

### Required frontmatter fields

Every concept file (every `.md` file that is not `index.md`, `log.md`, or
`template.md`) must include:

- `id`: a stable, globally unique identifier for the concept (e.g.
  `STD-U-001`, `ADR-007`, `WF-onboarding`).
- `type`: one of `standard`, `decision`, `workflow`, `project`, `memory`,
  `reference`.
- `status`: one of `draft`, `active`, `deprecated`, `superseded`,
  `proposed`, `accepted`, `rejected`.
- `tier`: one of `T1`, `T2`, `T3`, `T4`, `meta`.

### Recommended frontmatter fields

Including these fields improves search quality and the generated index:

- `title`: human-readable name.
- `description`: one-sentence summary.
- `tags`: list of keywords.
- `timestamp`: ISO 8601 date the document was last meaningfully updated.
- `applies_when`: list of trigger phrases describing the coding intents this
  document governs (see below).

See `SPEC.md` for the full schema, cross-linking conventions, and tier
definitions.

### Authoring `applies_when`

`applies_when` is a list of short verb phrases in an agent's own vocabulary,
not the document's internal terminology: `"reading a .env file"`, not
`"security"`. It matters because it is the highest-weight field in the
reference search index, tied with `title`, ahead of `description` and body
text: a standard titled "Secrets Handling" is retrieved when an agent is
mid-task on "logging a request body" only because that phrase is listed in
`applies_when`, not because the title or body happen to share vocabulary
with the query.

```yaml
applies_when:
  - "reading a .env file"
  - "committing a credential or API key"
  - "logging a request or response body"
```

Add it to every `standard` and `decision` document you expect an agent to hit
mid-task, phrased as that moment, not as a topic label.

### Reserved files

Three filenames have special meaning and must not carry concept frontmatter:

- `index.md`: generated navigation index (root index carries `spec_version`
  and `okf_version` frontmatter; see below).
- `log.md`: human-maintained changelog for the bundle, date-grouped,
  newest entry first.
- `template.md`: an authoring-scaffold file for a directory (not itself a
  governed concept).

### Cross-links

Reference other concepts with bundle-relative paths (absolute from the bundle
root, e.g. `/universal/foundation/STD-U-001.md`). This applies to markdown
links in the document body.

The `supersedes` and `superseded_by` frontmatter fields are different: they
hold a concept **ID** (or list of IDs), not a path (e.g. `supersedes: ADR-002`,
not `supersedes: /decisions/ADR-002.md`). These fields are not resolved or
validated by the current tooling; they are a documented convention for human
and agent readers tracing decision history, not a machine-checked chain. See
`SPEC.md` section 4.2.

### Directory structure

There is no required directory layout beyond the reserved file names. The
`example-bundle/` convention (tiers as top-level directories: `universal/`,
`tech-stacks/`, `decisions/`, `workflows/`, `projects/`) is a proven pattern
and is described in `SPEC.md`, but you can adapt it to your context.

## 3. Validate

```bash
uv run data-olympus lint <your-bundle-dir>
```

Expected output when the bundle is conformant (the trailing count is how many
concept files were linted; `N` is the number of concept docs in your bundle):

```
0 errors across 0 files (N linted)
```

The linter checks required frontmatter fields, controlled-vocabulary values,
and reserved-file constraints. Fix any reported errors before proceeding.
`lint` exits non-zero when it finds no concept files to lint (for example, an
empty or mis-pathed bundle), so a clean pass always means real files were
checked.

## 4. Generate navigation and graph

```bash
uv run data-olympus index <your-bundle-dir>
```

This regenerates `index.md` files at each directory level. The root
`index.md` carries `spec_version` and `okf_version` frontmatter; preserve
those lines if you edit the root index by hand.

```bash
uv run data-olympus visualize <your-bundle-dir> -o <your-bundle-dir>/viz.html --name "My KB"
```

This generates an interactive HTML graph of all concepts and their
cross-links. Open the file in a browser to explore the knowledge graph.

## 5. Serve to agents

### Local (development)

```bash
./scripts/run-local.sh
```

The script copies `example-bundle/` to `/tmp/data-olympus-demo-kb`, git-inits
the copy, and starts the MCP server at `http://localhost:8080`. It is a demo
helper only: it always wipes and recreates its target directory, so it does
not read `KB_MAIN_PATH` and must not be pointed at your own bundle.

To serve your own bundle instead, invoke the MCP server directly:

```bash
KB_MAIN_PATH=/path/to/your-bundle \
  KB_INDEX_PATH=/tmp/your-kb.db \
  KB_REMOTE_URL="" \
  uv run data-olympus-mcp
```

Your bundle must be a git repository (run `git init` inside it first).

### Docker

```bash
docker compose -f deploy/docker/compose.yaml up --build
```

Bind-mount your bundle to serve real content:

```bash
docker compose -f deploy/docker/compose.yaml run \
  -e KB_MAIN_PATH=/my-kb \
  -v /path/to/your-bundle:/my-kb:ro \
  data-olympus-mcp
```

### Kubernetes

Apply the kustomize manifests in `deploy/k8s/`:

```bash
kubectl apply -k deploy/k8s/
sops exec-file deploy/k8s/secret.sops.yaml 'kubectl apply -f {}'
```

See `docs/serving.md` for the full serving model, including the
single-replica / single-writer constraint and how to run read-only replicas
for higher read throughput.

### Write and push

To enable the write pipeline (agent-proposed edits committed and pushed to a
remote), set two environment variables before starting the server:

- `KB_REMOTE_URL`: the git remote URL the server should push to (SSH or HTTPS).
- The deploy key or credential must be available to the server process (SSH
  key on disk, or a credential helper).

Without `KB_REMOTE_URL`, the server runs in read-only mode: the pull loop runs
but does not push, and the write pipeline is disabled. REST write routes
(`/api/v1/propose/*`, `/resolve/{id}`, `/onboarding/bootstrap`) return
`503 {"error": "write_pipeline_disabled"}` and the MCP write tools return
`{"status": "write_pipeline_disabled"}` rather than crashing. Read routes work
normally.

## 6. Wire an agent

Register the MCP endpoint with your agent. The server exposes MCP over
streamable HTTP at `http://<host>:8080/mcp` (or the port you configured).

### Easiest: the setup wizard

```bash
data-olympus setup
```

The wizard probes the endpoint, detects which of Claude Code, Codex, Gemini, and
OpenCode are installed, writes each agent's MCP registration (with a timestamped
backup of any file it edits, and idempotent re-runs), optionally installs the
enforcement hooks, and prints a doctor summary. `data-olympus setup --check` runs
the same summary read-only and never changes anything.

### Manual per-agent registration

If you prefer to wire agents by hand, use the CURRENT surface for each. These are
the same commands/files the wizard uses. Replace `http://localhost:8080` with
your endpoint.

**Claude Code** (use the `claude mcp` CLI; it writes `~/.claude.json`, not
`settings.json`):

```bash
claude mcp add --transport http data-olympus http://localhost:8080/mcp
```

**Codex** (use the `codex mcp` CLI; it writes `[mcp_servers.*]` in
`~/.codex/config.toml`):

```bash
codex mcp add data-olympus --url http://localhost:8080/mcp
```

**Gemini / Antigravity** (merge into `~/.gemini/settings.json`; note the explicit
`"type": "http"`):

```json
{
  "mcpServers": {
    "data-olympus": {
      "url": "http://localhost:8080/mcp",
      "type": "http"
    }
  }
}
```

**OpenCode** (no native remote-HTTP transport; wrap via `mcp-remote` under the
top-level `mcp` key in `~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "data-olympus": {
      "type": "local",
      "command": ["npx", "-y", "mcp-remote", "http://localhost:8080/mcp", "--allow-http"],
      "enabled": true
    }
  }
}
```

Restart each agent's session after registering. Verify by asking it to call
`kb_search`.

The `kb` CLI in `bin/kb` is a thin client for the same HTTP API. Set
`KB_ENDPOINT` and use it from the shell or from agent-invoked scripts:

```bash
export KB_ENDPOINT=http://localhost:8080
./bin/kb search "module structure"
./bin/kb get /tech-stacks/backend-nestjs/STD-BN-001-module-structure.md
./bin/kb outline
```

The CLI supports `-o plain` for machine-readable output and `-o json` for
structured output suitable for piping into other tools.
