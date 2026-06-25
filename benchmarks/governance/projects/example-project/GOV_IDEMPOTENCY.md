---
id: GOV_IDEMPOTENCY
type: workflow
status: active
tier: T3
title: idempotency
description: Governance rules for idempotency.
applies_when:
  - "idempotency key"
  - "deduplication id"
  - "at-most-once"
  - "retry safe"
---

# idempotency (current)

This document records the current governance decision for the idempotency area. It states the recommended approach, the rationale behind it, and how to request an exception.

## Decision

- Adopt the documented pattern for this area.
- Prefer the recommended approach over ad-hoc alternatives.
- Record any deviation in the project decision record.
- Consult this page before related work begins.
