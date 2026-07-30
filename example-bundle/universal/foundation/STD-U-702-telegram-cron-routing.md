---
id: STD-U-702
type: standard
status: active
tier: T1
title: Telegram Cron Job Delivery Routing
description: Knaisoma standard for routing Hermes cron job deliveries to appropriate Telegram group channels.
tags: [operations, cron, telegram, delivery, routing]
timestamp: 2026-07-30
applies_when:
  - "configuring Hermes cron job delivery targets"
  - "routing notification messages to Telegram groups"
  - "setting up new cron jobs with delivery channels"
validity:
  last_verified: 2026-07-30
  recheck_by: 2027-01-30
  verification_source: operational configuration review
---

# Rule

All Hermes cron job deliveries to Telegram groups MUST follow the Knaisoma routing convention: group specialization by notification type.

# Groups

## Enigmas Group

- **Chat ID**: `-5282998441`
- **Purpose**: Friday Enigmas puzzle distribution
- **Job**: Friday Enigmas cron job
- **Channel**: `telegram:-5282998441`

## Alerts Group

- **Chat ID**: `-3944487021`
- **Purpose**: Immediate attention alerts and system status
- **Jobs**: 
  - Weekly Operational Audit
  - Daily Free Model Check
- **Channel**: `telegram:-3944487021`

## General Projects Group

- **Chat ID**: `-3620245910`
- **Purpose**: General project activity and cross-project notifications
- **Jobs**:
  - Data Olympus Outreach
  - PianoCraft Lessons
  - Article Generator
  - Monthly Testimonials
  - Release Planner
  - Branch Scanner
- **Channel**: `telegram:-3620245910`

## Personal DM

- **Chat ID**: `230655120`
- **Purpose**: Direct personal notifications and config home channel
- **Channel**: `telegram:230655120`
- **Note**: This is the default/home channel configured in `config.yaml`

# Routing Table

| Job Name | Target Group | Chat ID | Channel |
|----------|-------------|---------|---------|
| Friday Enigmas | Enigmas | `-5282998441` | `telegram:-5282998441` |
| Weekly Operational Audit | Alerts | `-3944487021` | `telegram:-3944487021` |
| Daily Free Model Check | Alerts | `-3944487021` | `telegram:-3944487021` |
| Data Olympus Outreach | Projects | `-3620245910` | `telegram:-3620245910` |
| PianoCraft Lessons | Projects | `-3620245910` | `telegram:-3620245910` |
| Article Generator | Projects | `-3620245910` | `telegram:-3620245910` |
| Monthly Testimonials | Projects | `-3620245910` | `telegram:-3620245910` |
| Release Planner | Projects | `-3620245910` | `telegram:-3620245910` |
| Branch Scanner | Projects | `-3620245910` | `telegram:-3620245910` |

# Why

Cron job delivery routing was previously failing with `Chat not found` errors because legacy Telegram group IDs were no longer valid. Explicit group ID mapping ensures reliable delivery to the correct teams.

# Notes

- This configuration is temporary and subject to revision.
- Future: Consider per-project group routing for larger teams.
- The `deliver` field in cron job configuration uses the format `telegram:<chat_id>`.
- Never include API tokens or sensitive group metadata in KB rules — only chat IDs and routing logic.
