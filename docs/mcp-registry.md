# Official MCP Registry notes

Data Olympus is not published in the official registry at
<https://registry.modelcontextprotocol.io> yet. Two searches of its v0 API,
`?search=data-olympus` and `?search=knaisoma`, returned zero servers on 2026-09-12.

This file records what publication needs, so the work is a checklist rather than a
research task. It is the registry equivalent of [`glama.md`](./glama.md).

## Why it matters

`modelcontextprotocol/servers` no longer accepts new server implementations. Its
CONTRIBUTING directs authors to the registry, and its README tells readers looking for
a list of servers to browse the registry rather than that repository.

## What is in this repository

[`server.json`](../server.json) at the repository root declares the server:

- `name` is `io.github.knaisoma/data-olympus`. The `io.github.*` namespace authenticates
  through GitHub, so no DNS verification is needed.
- The package entry is `registryType: pypi`, identifier `data-olympus`, against
  `https://pypi.org`, which is on the registry's list of accepted package sources.
- The transport is `streamable-http` at `http://localhost:8080/mcp`, which is what
  `data-olympus-mcp` actually serves and what [`adoption.md`](./adoption.md) documents.
  This server is not stdio.
- `version` tracks the released version the entry describes, so it moves with a release
  rather than with every commit.

`README.md` carries `<!-- mcp-name: io.github.knaisoma/data-olympus -->` below the
badges. That string is how the registry verifies package ownership: it looks for
`mcp-name: $SERVER_NAME` in the README that PyPI serves as the package description.

## What is not done, and why

**The marker is not on PyPI yet.** This change adds the marker to `README.md`; the
published description for 0.7.3 lacks it. Publication therefore has to follow a release
that ships this README.

**The package and the MCP executable have different names.** The package is
`data-olympus`, and the console script that starts the MCP server is `data-olympus-mcp`,
alongside the `data-olympus` CLI. A consumer running `uvx data-olympus` would get the
CLI, so the explicit form is:

```bash
uvx --from 'data-olympus==<release>' data-olympus-mcp
```

`server.json` deliberately declares no runtime launch metadata. The transport block
describes where the server listens once somebody starts it, which matches how this
project is deployed: the operator runs it against their own knowledge bundle. If
automated launch is wanted later, that is a deliberate addition of `runtimeHint` and
runtime arguments, and it should be tested against a real client rather than assumed
from a schema that validates.

**Publication authenticates as a person or as CI.** The `mcp-publisher` flow signs in
with GitHub OAuth for an `io.github.*` namespace, or uses GitHub OIDC when it runs from
Actions. Neither path is a file change, so it is a deliberate step by a maintainer or a
workflow, not something a docs PR completes.

## Checklist for whoever publishes

1. Cut a release whose PyPI description contains the `mcp-name` marker.
2. Set **both** version fields in `server.json` to that release: the top-level `version`
   and `packages[0].version`. Leaving the package pinned to an older version points the
   entry at a description that does not carry the marker. Then re-validate the file
   against the published schema.
3. Confirm that the pinned release's own description carries the exact marker. Run from
   the repository root:

   ```bash
   set -euo pipefail
   release=$(jq -er '.packages[0].version' server.json)
   server_name=$(jq -er '.name' server.json)
   curl -fsS "https://pypi.org/pypi/data-olympus/${release}/json" |
     jq -e --arg marker "<!-- mcp-name: ${server_name} -->" \
       '(.info.description // "") | contains($marker)'
   ```

   It reads the version-specific endpoint rather than `/pypi/data-olympus/json`, which
   returns the latest release, and it matches the complete comment rather than the
   fragment `mcp-name` anywhere in the response. Today it prints `false` and exits 1,
   because 0.7.3 predates the marker.
4. Authenticate: `mcp-publisher login github`, or publish from Actions with OIDC.
5. Publish, then confirm the entry resolves by searching the registry API for
   `data-olympus`.
6. Record the resulting registry name here.

## Status of the registry itself

The registry's [development status](https://github.com/modelcontextprotocol/registry#development-status)
reports an API freeze for v0.1 dated 2025-10-24, while development continues on v0, and a
preview launch dated 2025-09-08 that warns of possible breaking changes or data resets.
Treat an entry as something to re-verify after upstream changes rather than as permanent.
