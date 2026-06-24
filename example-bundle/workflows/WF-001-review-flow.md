---
id: WF-001
type: workflow
status: active
tier: meta
title: Knowledge Base Review Flow
description: Step-by-step process for proposing, reviewing, and committing changes to the Acme knowledge bundle.
tags: [workflow, review, knowledge-base, agents]
timestamp: 2026-06-24
---
# Purpose

This workflow describes how engineers and agents propose, review, and commit
changes to the knowledge bundle. It applies the review criteria from
[STD-U-002](/universal/foundation/STD-U-002-code-review.md).

# Steps

1. **Propose.** Use `kb propose memory <text>` (new concept) or
   `kb propose edit <path>` (existing concept) to create a pending entry.
2. **Review.** A human or designated reviewer reads the diff and the rationale.
3. **Decide.** Use `kb resolve <pending-id> --decision approve|reject`.
   - Approved: the MCP commits the change and pushes to the remote.
   - Rejected: the pending entry is discarded with a reason.
4. **Confirm.** Run `kb search <topic>` to verify the change is indexed.

# Notes

- Agents operate under the single-writer constraint described in ADR-002
  (see [ADR-002](/decisions/ADR-002-single-writer-serving.md)).
- Low-confidence proposals pause in `pending_confirmation` state until a
  human approves interactively.
- Emergency rollback: `git revert <sha>` in the bundle repository, then
  restart the MCP server to rebuild the index.
