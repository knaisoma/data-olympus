---
id: OKF-RETRY
kind: standard
state: active
name: Retry policy for outbound calls
keywords: [reliability, networking]
---
# Retry policy

All outbound HTTP calls use exponential backoff with jitter. The base delay is
100ms and the cap is 30s. Idempotent requests may be retried up to five times;
non-idempotent requests are never retried automatically.
