---
id: GOV_NEW_GRACEFUL_SHUTDOWN
type: standard
status: active
tier: T1
title: graceful-shutdown (current)
description: Current graceful-shutdown governance.
applies_when:
  - "sigterm"
  - "drain connections"
  - "shutdown hook"
  - "preStop"
supersedes: GOV_OLD_GRACEFUL_SHUTDOWN
---

# graceful-shutdown (current)

This document records the current governance decision for the graceful-shutdown area. It states the recommended approach, the rationale behind it, and how to request an exception.

## Decision

- Adopt the documented pattern for this area.
- Prefer the recommended approach over ad-hoc alternatives.
- Record any deviation in the project decision record.
- Consult this page before related work begins.
