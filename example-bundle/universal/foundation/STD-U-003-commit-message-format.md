---
id: STD-U-003
type: standard
status: superseded
tier: T1
title: Commit Message Format (v1)
description: Original commit message convention for Acme repositories. Replaced by STD-U-004, which adds a required scope segment.
tags: [git, commits, superseded]
timestamp: "2026-05-10"
superseded_by: STD-U-004
---
# Rule

Commit messages use a free-text subject line under 72 characters, optionally
followed by a blank line and a longer body.

```
Fix the login redirect loop
```

# Why

This was Acme's first attempt at a commit message convention. It did not
distinguish feature work from fixes or chores, which made it hard to
generate changelogs automatically.

# Status

Superseded by [STD-U-004](STD-U-004-commit-message-format.md), which requires
a Conventional-Commits-style `type(scope): subject` header so tooling can
categorize commits without parsing prose.
