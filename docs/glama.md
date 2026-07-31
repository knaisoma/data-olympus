# Glama registry notes

Data Olympus is listed on Glama at:

- <https://glama.ai/mcp/servers/knaisoma/data-olympus>
- <https://glama.ai/mcp/servers/knaisoma/data-olympus/schema>
- <https://glama.ai/mcp/servers/knaisoma/data-olympus/score>

## Claim metadata

Repository ownership is declared in the root [`glama.json`](../glama.json).
For an organization-owned repository, Glama requires this file so a GitHub user
with repository access can claim and manage the server listing.

## Release setup

Glama only scores the tool surface after it can build, start, and introspect the
MCP server. The Data Olympus production image is built from the repository root
with a non-root Dockerfile path:

- Build context: repository root
- Dockerfile path: `deploy/docker/Dockerfile`
- HTTP port: `8080`
- MCP endpoint: `/mcp`

For Glama introspection, do not point the release at a real operator knowledge
base. Use the repository's bundled `example-bundle` corpus. It is safe to expose,
it lets startup build a SQLite index with a schema, and it gives catalog
inspectors valid results for read tools instead of an empty database or a
database without tables.

Suggested sandbox environment:

```text
KB_MAIN_PATH=/example-bundle
KB_INDEX_PATH=/index/kb.db
KB_REMOTE_URL=
KB_GIT_REMOTE_URL=
KB_HTTP_PORT=8080
```

Leave `KB_AUTH_TOKEN` and `KB_AUTH_PRINCIPALS` unset for the public sandbox so
Glama can introspect the schema. Do not mount deploy keys or production state
into the Glama release.

## Score maintenance

Glama derives the score from the running server, not from README prose alone.
The highest-impact repo-side checks are:

- Keep `glama.json` valid and merged to `main`.
- Keep the Glama score badge in the README so downstream catalog PRs can link
  the canonical server entry.
- Keep MCP tool annotations accurate: read-only tools must be marked read-only,
  write/proposal tools must not be.
- Keep every MCP parameter schema described. Glama's TDQS rubric uses schema
  description coverage when scoring parameter semantics.
- Keep tool descriptions concise and sibling-aware: say when to use a tool and
  which neighboring tool to use instead.
- After changing MCP tool descriptions, titles, annotations, or parameter
  schemas, trigger a new Glama release or rescan so the public score updates.

## Maintenance profile actions

The 0.6.0 Glama readiness pass addresses the remaining profile gaps tracked in
GitHub issue #155.

Completed repository actions:

- `glama.json` declares related servers: `knowledge-mcp`, `en-quire`,
  `md-graph`, and `obsidian-kb`.
- Public issue triage comments record maintainer responses for the current open
  backlog entries that lacked one.
- The public sandbox image uses `example-bundle`, so usage seeding can exercise
  real read tools without exposing an operator knowledge base.

Manual follow-up after the release branch merges:

- Trigger a Glama rescan or new release.
- Use Glama "Try in Browser" against the rebuilt sandbox and call `kb_health`,
  `kb_search`, `kb_get`, `kb_list`, `kb_outline`, `kb_onboarding_status`, and
  `kb_cleanup_plan`.
- Recheck the score page and confirm Maintenance grade A and profile completion
  100 percent.
