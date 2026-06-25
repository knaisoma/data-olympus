---
id: GOV_OLD_GRACEFUL_SHUTDOWN
type: standard
status: superseded
tier: T1
title: graceful-shutdown (old)
description: Previous graceful-shutdown governance (superseded).
applies_when:
  - "sigterm"
  - "drain connections"
  - "shutdown hook"
  - "preStop"
superseded_by: GOV_NEW_GRACEFUL_SHUTDOWN
---

# graceful-shutdown (previous)

This document records the previous governance decision for the graceful-shutdown area. It states the recommended approach, the rationale behind it, and how to request an exception.

## Decision

- Adopt the documented pattern for this area.
- Prefer the recommended approach over ad-hoc alternatives.
- Record any deviation in the project decision record.
- Consult this page before related work begins.
