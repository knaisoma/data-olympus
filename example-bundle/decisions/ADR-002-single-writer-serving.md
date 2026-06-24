---
id: ADR-002
type: decision
status: accepted
tier: meta
title: Use single-writer MCP serving model
description: Acme runs one write-enabled data-olympus MCP replica to prevent concurrent-write races on the knowledge bundle.
tags: [architecture, mcp, serving, concurrency]
timestamp: 2026-06-24
supersedes: []
---
# Context

Multiple agents may propose writes to the knowledge bundle concurrently.
Without coordination, concurrent writes can corrupt the git working tree
or produce conflicting commits.

See the original adoption decision in [ADR-001](/decisions/ADR-001-use-data-olympus.md).
The secrets handling standard in [STD-U-601](/universal/security/STD-U-601-secrets.md)
applies to the deploy key used by the write pipeline.

# Decision

Run exactly one write-enabled data-olympus MCP replica. That replica holds
advisory per-path locks and a durable push queue that serializes outbound
git pushes.

Read-only replicas (serving `kb_search`, `kb_get`, `kb_list`, `kb_outline`)
may scale horizontally because they do not write.

# Consequences

- No concurrent-write race condition on the bundle.
- Write throughput is bounded by the single replica, which is acceptable for
  curated KB workloads where writes are infrequent.
- The deploy key for the write replica is a sensitive credential and is
  governed by STD-U-601.
