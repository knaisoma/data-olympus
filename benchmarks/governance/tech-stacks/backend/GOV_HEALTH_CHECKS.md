---
id: GOV_HEALTH_CHECKS
type: decision
status: accepted
tier: T2
title: health-checks
description: Governance rules for health-checks.
applies_when:
  - "liveness probe"
  - "readiness probe"
  - "healthz endpoint"
  - "k8s probe"
---

# health-checks (current)

This document records the current governance decision for the health-checks area. It states the recommended approach, the rationale behind it, and how to request an exception.

## Decision

- Adopt the documented pattern for this area.
- Prefer the recommended approach over ad-hoc alternatives.
- Record any deviation in the project decision record.
- Consult this page before related work begins.
