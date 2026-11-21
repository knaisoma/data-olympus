# Upgrading to data-olympus 0.12

This release changes three things that can break an existing deployment. MCP
clients see no difference: tool names, arguments and responses are unchanged.
Check each section below that matches how you run the server.

## The container image is based on Alpine

The runtime image moves from `python:3.13-slim` (Debian) to
`python:3.15-alpine` (Alpine 3.24, musl libc, Python 3.15), pinned by digest.

Who is affected: anyone who builds an image `FROM` the data-olympus image, or
runs package commands inside the container.

What to do:

- Replace `apt-get install` with `apk add --no-cache`. Package names differ in
  places; look them up on the Alpine package index.
- A derived image that installs Python packages with native code must use
  wheels built for musl (`musllinux`), or build them from source.
- The server still runs as uid 65534 and the git transport policy is
  unchanged: only SSH and local remotes are allowed.

## The container command changed

The image command is now the installed console script
`/app/.venv/bin/data-olympus-mcp`, still started through the entrypoint.
`uv` is no longer in the runtime image.

Who is affected: anyone who overrides the container command, for example a
Kubernetes `command` or `args`, a Compose `command`, or a `docker run` that
passes `uv run data-olympus-mcp`.

What to do: remove the override to use the image default, or replace
`uv run data-olympus-mcp` (and `uv run --no-sync data-olympus-mcp`) with
`/app/.venv/bin/data-olympus-mcp`. Health and readiness probes are unchanged.

## FastMCP 4 and MCP SDK 2

The server now requires `fastmcp>=4.1.1,<5` and `mcp>=2.3.0,<3`.

Who is affected: anyone who installs data-olympus from PyPI into an
environment that also pins FastMCP 3 or MCP SDK 1, or who imports `fastmcp`
or `mcp` from the same environment in their own code.

What to do: move those pins and imports to FastMCP 4 and MCP SDK 2, or install
data-olympus in its own environment.

## Python 3.15 and the embeddings extra

Python 3.13, 3.14 and 3.15 are supported. On Python 3.15 the `embeddings`
extra installs nothing, because onnxruntime publishes no wheels for it yet,
and `KB_EMBEDDINGS_MODE=on` then fails startup. Leave `KB_EMBEDDINGS_MODE`
unset on 3.15, or run on 3.13 or 3.14 if you need embeddings from a PyPI
install. The container image never
installed that extra, so it is unaffected.
