---
id: REF-acme-app-rest-endpoints
type: reference
status: active
tier: meta
title: Acme App API endpoint reference
description: Enumerates the REST endpoints exposed by the Acme App API component, for lookup rather than governance.
tags: [reference, api, acme-app]
timestamp: "2026-06-24"
---
# Acme App API endpoints

This is a lookup reference, not a standard: it describes what the API
currently exposes, not a rule the API must follow. See
[STD-BN-001](/tech-stacks/backend-nestjs/STD-BN-001-module-structure.md) for
the module conventions the implementation follows, and
[COMP-acme-app-api](/projects/acme-app/components/api/README.md) for
component-level context.

| Method | Path                | Module   | Notes                          |
|--------|---------------------|----------|--------------------------------|
| GET    | `/health`           | health   | Liveness probe, no auth        |
| POST   | `/auth/login`       | auth     | Returns a session token        |
| POST   | `/auth/logout`      | auth     | Invalidates the session token  |
| GET    | `/users/:id`        | users    | Requires an authenticated user |
| PATCH  | `/users/:id`        | users    | Requires the user themself     |

# Why this is a `reference` document, not a `standard`

A `reference` document describes current state for lookup (what exists
today); a `standard` prescribes a rule that governs future work. This table
will drift out of date as the API changes, which is acceptable for a
reference doc in a way it would not be for a standard: nothing in this file
asserts what SHOULD be true, only what currently IS.
