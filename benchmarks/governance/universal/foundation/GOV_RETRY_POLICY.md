---
id: GOV_RETRY_POLICY
type: standard
status: active
tier: T1
title: retry-policy
description: Governance rules for retry-policy.
applies_when:
  - "exponential backoff"
  - "jitter"
  - "max attempts"
  - "retry budget"
---

# retry-policy (current)

This document records the current governance decision for the retry-policy area. It states the recommended approach, the rationale behind it, and how to request an exception.

## Decision

- Adopt the documented pattern for this area.
- Prefer the recommended approach over ad-hoc alternatives.
- Record any deviation in the project decision record.
- Consult this page before related work begins.
