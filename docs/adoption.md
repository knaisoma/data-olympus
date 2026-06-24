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

Every concept file (every `.md` file that is not `index.md` or `log.md`) must
include:

- `id`: a stable, globally unique identifier for the concept (e.g.
  `STD-U-001`, `ADR-007`, `WF-onboarding`).
- `type`: one of `standard`, `decision`, `workflow`, `project`, `component`.
- `status`: one of `draft`, `proposed`, `accepted`, `deprecated`,
  `superseded`.
- `tier`: one of `T1`, `T2`, `T3`, `T4`, `meta`.

### Recommended frontmatter fields

Including these fields improves search quality and the generated index:

- `title`: human-readable name.
- `description`: one-sentence summary.
- `tags`: list of keywords.
- `timestamp`: ISO 8601 date the document was last meaningfully updated.

See `SPEC.md` for the full schema, cross-linking conventions, and tier
definitions.

### Reserved files

Two filenames have special meaning and must not carry concept frontmatter:

- `index.md`: generated navigation index (root index carries `spec_version`
  and `okf_version` frontmatter; see below).
- `log.md`: human-maintained changelog for the bundle, date-grouped,
  newest entry first.

### Cross-links

Reference other concepts with bundle-relative paths (absolute from the bundle
root, e.g. `/universal/foundation/STD-U-001.md`). The `supersedes` and
`superseded_by` frontmatter fields also accept these paths for `decision` type
documents.

### Directory structure

There is no required directory layout beyond the reserved file names. The
`example-bundle/` convention (tiers as top-level directories: `universal/`,
`tech-stacks/`, `decisions/`, `workflows/`, `projects/`) is a proven pattern
and is described in `SPEC.md`, but you can adapt it to your context.

## 3. Validate

```bash
uv run data-olympus lint <your-bundle-dir>
```

Expected output when the bundle is conformant:

```
0 errors across 0 files
```

The linter checks required frontmatter fields, controlled-vocabulary values,
and reserved-file constraints. Fix any reported errors before proceeding.

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
the copy, and starts the MCP server at `http://localhost:8080`. To serve your
own bundle instead, set `KB_MAIN_PATH` before running:

```bash
KB_MAIN_PATH=/path/to/your-bundle ./scripts/run-local.sh
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
but does not push, and write tools return an appropriate error.

## 6. Wire an agent

Register the MCP endpoint with your agent. The server exposes MCP over
streamable HTTP at `http://<host>:8080/mcp` (or the port you configured).

For Claude Code, add an entry to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "my-kb": {
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

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
