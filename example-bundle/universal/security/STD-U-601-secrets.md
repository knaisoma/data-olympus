---
id: STD-U-601
type: standard
status: active
tier: T1
title: Secrets Handling
description: Universal rules for handling credentials, API keys, and other secrets across all Acme projects.
tags: [security, secrets, credentials]
timestamp: 2026-06-24
---
# Purpose

This standard ensures secrets never appear in source control, agent context,
or log output.

# Scope

Applies to all engineers, automated agents, scripts, and CI/CD pipelines
at Acme.

# Rules

- Never write credentials, API keys, or passwords to any file tracked by git.
- Retrieve secrets at runtime from the designated secrets management system,
  not from environment files committed to the repository.
- Do not paste secret values into chat sessions, commit messages, PR bodies,
  or plan documents.
- Before reading any `.env`, `*secret*`, or `*credentials*` file, confirm
  the file is not tracked by git (`.gitignore` check).
- Rotate any secret that was accidentally exposed within 24 hours of detection.

# Why

Secrets committed to source control are effectively public once the repository
is cloned or leaked. Rotation and runtime retrieval limit blast radius.
