---
id: COMP-acme-app-api
type: project
status: active
tier: T4
title: Acme App API Component
description: Component-level knowledge for the Acme App REST API, including module conventions and deployment notes.
tags: [acme-app, api, nestjs, component]
timestamp: 2026-06-24
---
# Acme App API Component

The API component is the primary HTTP surface for the Acme App. It is built
with NestJS following the module conventions in
[STD-BN-001](/tech-stacks/backend-nestjs/STD-BN-001-module-structure.md).

For project-level context, see the
[Acme App project overview](/projects/acme-app/README.md).

# Module Layout

```
src/
  health/
  auth/
  users/
  knowledge/
```

Each module follows the STD-BN-001 directory layout (module, controller,
service, dto/, __tests__/).

# Deployment

The API is deployed as a single container. Environment variables are injected
at runtime. No secrets are committed to the repository (see STD-U-601 via the
project-level knowledge).

# Key Decisions

- REST over gRPC for external clients (simpler tooling for web consumers).
- NestJS chosen for its first-class TypeScript support and module system.
