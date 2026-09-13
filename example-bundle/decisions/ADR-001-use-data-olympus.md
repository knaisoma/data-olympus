---
id: ADR-001
type: decision
status: accepted
tier: meta
title: Adopt data-olympus for the knowledge base
description: Acme stores its knowledge as a data-olympus bundle.
tags: [architecture]
generated:
  by: human:data-olympus-maintainers
  at: "2026-06-24T00:00:00Z"
---
# Context
Acme needs a portable, agent-readable knowledge base.

# Decision
Adopt the data-olympus format and serve it via the single-writer MCP server.
See [STD-U-001](/universal/foundation/STD-U-001-writing-style.md).

# Consequences
Knowledge is version-controlled and queryable by any agent.
