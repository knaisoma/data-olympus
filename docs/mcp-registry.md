# Official MCP Registry notes

Data Olympus is not published in the official registry at
<https://registry.modelcontextprotocol.io> yet. A search of its v0 API,
`?search=data-olympus`, returned zero servers on 2026-10-01.

From the release after 0.10.0, the stable promotion workflow publishes the entry
itself (see "Automated publication" below). This file records what publication
needs and keeps the manual checklist as a fallback. It is the registry equivalent of
[`glama.md`](./glama.md).

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

**Registry publication requires the marker-bearing package on PyPI.** This change adds
the marker to `README.md`. Every description published up to and including 0.7.3 predates
it; this release includes it. Publish the registry entry only after the version-specific
check below succeeds.

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
Actions. A person can publish under `io.github.knaisoma/*` only if their membership of
the organization is public; otherwise the registry grants only their personal
namespace and refuses the publish with 403. OIDC authorizes by the repository owner, so
the workflow does not depend on anyone's membership visibility.

## Automated publication

The `publish-mcp-registry` job in `.github/workflows/tag-release.yml` (issue #303) runs
after the GitHub release, from the released source revision. It:

1. refuses unless both `server.json` versions equal the released version;
2. reads that version's PyPI description and refuses unless it carries the exact
   `mcp-name` marker (the check in step 3 below);
3. installs a pinned `mcp-publisher` release and verifies its SHA256 before running it;
4. signs in with `mcp-publisher login github-oidc` and publishes, skipping the publish
   when the registry already lists that version, so a re-run is safe;
5. reads the entry back from the registry API, filtered by version, and fails unless it
   is listed.

It runs last on purpose: a registry outage cannot hold back PyPI, the image or the
GitHub release, and a failure is visible in the run and can be re-run on its own. To
move to a newer publisher, change `MCP_PUBLISHER_VERSION` and `MCP_PUBLISHER_SHA256`
together, taking the digest from the registry release's published asset.

## Manual checklist (fallback)

1. Cut a release whose PyPI description contains the `mcp-name` marker.
2. Set **both** version fields in `server.json` to that release: the top-level `version`
   and `packages[0].version`. Leaving the package pinned to 0.7.3 or earlier points the
   entry at a description that does not carry the marker. Then re-validate the file
   against the published schema. Both fields are pinned to `0.10.0`, so this step is
   already done for that release and is only owed again on the next one.
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
   fragment `mcp-name` anywhere in the response. It prints `false` and exits 1 for any
   version up to 0.7.3, which predate the marker, and is expected to pass once 0.8.0 is
   on PyPI. Run it rather than assuming it.
4. Authenticate: `mcp-publisher login github`, or publish from Actions with OIDC.
5. Publish, then confirm the entry resolves by searching the registry API for
   `data-olympus`.
6. Record the resulting registry name here.

## Status of the registry itself

The registry's [development status](https://github.com/modelcontextprotocol/registry#development-status)
reports an API freeze for v0.1 dated 2025-10-24, while development continues on v0, and a
preview launch dated 2025-09-08 that warns of possible breaking changes or data resets.
Treat an entry as something to re-verify after upstream changes rather than as permanent.
