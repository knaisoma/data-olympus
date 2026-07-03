---
id: STD-U-004
type: standard
status: active
tier: T1
title: Commit Message Format
description: Conventional-Commits-style commit message convention for Acme repositories, replacing the free-text convention in STD-U-003.
tags: [git, commits]
timestamp: "2026-06-24"
supersedes: STD-U-003
applies_when:
  - "writing a commit message"
  - "opening a pull request"
---
# Rule

Commit messages use a Conventional Commits header:

```
type(scope): subject
```

- `type` is one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.
- `scope` is the affected module or component (required, unlike the retired
  STD-U-003 convention).
- `subject` is an imperative, present-tense description under 72 characters.

```
fix(auth): stop redirect loop on expired session
```

# Why

A required `scope` segment lets tooling (changelog generators, release notes)
group commits by area without parsing prose. This replaces
[STD-U-003](STD-U-003-commit-message-format.md), which allowed a free-text
subject with no required structure.

# Migration

Existing history written under STD-U-003 is not rewritten. The new format
applies to all commits from 2026-06-24 onward.
