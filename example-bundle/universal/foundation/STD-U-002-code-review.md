---
id: STD-U-002
type: standard
status: active
tier: T1
title: Code Review Standard
description: Rules for conducting and recording code reviews at Acme so that quality gates are consistent and auditable.
tags: [foundation, code-review, quality]
timestamp: 2026-06-24
---
# Purpose

This standard defines the mandatory steps for reviewing code at Acme.
It builds on the general writing principles in [STD-U-001](/universal/foundation/STD-U-001-writing-style.md).

# Scope

Applies to all engineers and automated agents that submit or review pull requests.

# Rules

- Every pull request requires at least one human approval before merge.
- Automated agents may leave review comments but may not approve alone.
- Security-sensitive changes (auth, RLS, PII, secrets) require a second human reviewer.
- Feedback follows PROBLEM / WHY / BETTER APPROACH / BENEFITS format.
- Reviewer records verdict as: **Approved**, **Request changes**, or **Comment only**.

# Why

Consistent review criteria reduce merge risk and create an auditable record of
why changes were accepted.
