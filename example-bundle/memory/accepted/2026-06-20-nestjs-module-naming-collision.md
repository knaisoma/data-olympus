---
id: MEM-2026-06-20-nestjs-module-naming-collision
type: memory
status: active
tier: meta
title: NestJS module naming collision with the shared `users` package
description: A recorded incident where a new `users` feature module collided with an existing shared package of the same name, and the resolution the team settled on.
tags: [nestjs, memory, incident]
timestamp: "2026-06-20"
applies_when:
  - "adding a new NestJS feature module named users"
  - "naming a module that might collide with a shared package"
---
# What happened

While adding a `users` feature module to the Acme App API component (see
[COMP-acme-app-api](/projects/acme-app/components/api/README.md)), an engineer
named the module directory `src/users/`, which collided with an existing
internal shared package also named `users` published to the private npm
registry. The collision was caught in CI when the bundler resolved the wrong
`users` import.

# Resolution

Feature modules that would collide with an existing shared package name are
suffixed with `-module` (e.g. `src/users-module/`) rather than renamed to
something unrelated, so the directory name still reads clearly next to
[STD-BN-001](/tech-stacks/backend-nestjs/STD-BN-001-module-structure.md)'s
naming conventions.

# Why this is recorded as memory, not a standard

This is a one-off naming collision specific to Acme's current package set, not
a general rule every NestJS project needs. If the same collision recurs across
multiple projects, promote this into an amendment to STD-BN-001 instead of
leaving it as a memory entry.
