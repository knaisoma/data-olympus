---
id: GOV_DATABASE_MIGRATIONS
type: standard
status: active
tier: T1
title: database-migrations
description: Governance rules for database-migrations.
applies_when:
  - "flyway"
  - "alembic"
  - "migration script"
  - "schema version"
---

# database-migrations (current)

This document records the current governance decision for the database-migrations area. It states the recommended approach, the rationale behind it, and how to request an exception.

## Decision

- Adopt the documented pattern for this area.
- Prefer the recommended approach over ad-hoc alternatives.
- Record any deviation in the project decision record.
- Consult this page before related work begins.
