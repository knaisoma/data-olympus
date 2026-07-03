# 4. Use a distributed SQL database

Date: 2024-05-15

## Status

Accepted

Supersedes ADR-0002

## Context

Write throughput outgrew a single PostgreSQL primary.

## Decision

We will migrate to a distributed SQL database that speaks the PostgreSQL wire
protocol so application code changes little.

## Consequences

Horizontal write scaling is possible; operational complexity increases.
