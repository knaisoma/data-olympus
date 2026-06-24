---
id: STD-BN-001
type: standard
status: active
tier: T2
title: NestJS Module Structure
description: Prescribes the directory layout and naming conventions for NestJS modules in Acme backend services.
tags: [nestjs, backend, architecture, typescript]
timestamp: 2026-06-24
---
# Purpose

This standard defines how NestJS modules are structured in all Acme backend
services. It inherits the general writing and documentation principles from
[STD-U-001](/universal/foundation/STD-U-001-writing-style.md).

# Scope

All backend services built with NestJS.

# Required Directory Layout

Each feature module lives under `src/<feature>/` and contains:

- `<feature>.module.ts` (module declaration)
- `<feature>.controller.ts` (HTTP layer)
- `<feature>.service.ts` (business logic)
- `<feature>.repository.ts` (data access, if needed)
- `dto/` (request and response DTOs)
- `__tests__/` (unit and integration tests co-located with the module)

# Naming Conventions

- File names use kebab-case.
- Class names use PascalCase matching the file name.
- Module, controller, and service names end in `Module`, `Controller`,
  `Service` respectively.

# Why

Consistent structure lets engineers navigate any service without a mental
context switch. Agents can resolve symbols across repositories using the
same lookup pattern.
