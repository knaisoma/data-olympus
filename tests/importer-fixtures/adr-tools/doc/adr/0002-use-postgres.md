# 2. Use PostgreSQL for persistence

Date: 2024-02-01

## Status

Superseded by [ADR-0004](0004-use-distributed-sql.md)

## Context

The application needs a relational datastore for transactional workloads.

## Decision

We will use PostgreSQL as the primary datastore.

## Consequences

Operational familiarity is high; scaling writes horizontally is harder.
