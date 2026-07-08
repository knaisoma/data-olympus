# Serving model

This document describes the normative serving model for data-olympus. It
summarizes `SPEC.md` section 8; for the authoritative text, see that section.

## Single replica, streamable HTTP

A write-enabled data-olympus server runs as a **single replica** and exposes
MCP over **streamable HTTP**.

The write path is single-writer by design:

- Per-path advisory locks prevent two concurrent write operations from racing
  on the same concept file.
- Per-session git worktrees isolate in-flight proposed edits from the live
  index until they are committed.
- A durable push queue serializes outbound git pushes so no write is lost if
  the remote is briefly unavailable.

A single shared HTTP surface, rather than N independent stdio processes, gives
every agent one synchronized conversation with the server. When multiple agents
each run their own stdio MCP process against the same git working tree, they
race each other's worktrees and lock state. Streamable HTTP eliminates that
race.

### The long-lived SSE stream (proxy and session tuning)

Each MCP client holds a long-lived `GET /mcp` Server-Sent-Events stream to
receive server-to-client messages. Two things must be configured so that stream
does not churn:

- **Ingress/proxy read timeout.** A proxy that closes the idle stream (nginx's
  default is 60s) forces the client to reconnect on that cadence. Each reconnect
  creates a new transport server-side and, in a persistent client like
  `opencode serve`, leaks event listeners. Set a long read timeout and disable
  response buffering on the `/mcp` path (see `deploy/k8s/ingress.yaml` for the
  nginx annotations).
- **Session reaping.** FastMCP does not reap the transport a client leaves behind
  when it disconnects without sending `DELETE`. The server runs its own reaper
  (`session_metrics`): the activity middleware keeps a session non-idle for as
  long as its SSE stream is open, and `KB_SESSION_IDLE_TIMEOUT_SEC` (default
  `300`) terminates a session only after its stream has actually closed.
  `KB_SESSION_TOUCH_INTERVAL_SEC` (default `30`, clamped to a third of the idle
  window) sets how often an in-flight stream is re-stamped. `kb_health` /
  `/api/v1/health` and a periodic log surface the live session count.

## Core configuration reference

A few server-wide settings, beyond the feature-specific env vars documented in
their own sections below (search ranking, synonyms, embeddings, co-occurrence,
trigram, auth, audit rotation):

- `KB_HTTP_PORT`: TCP port the MCP HTTP server binds (default `8080`).
- `KB_CONFIDENCE_THRESHOLD`: the auto-commit confidence cutoff, in `[0, 1]`
  (default `0.85`). A proposal at or above it from a principal holding
  `auto_commit` is committed; below it (or from a principal without that
  capability) it is parked as pending. An out-of-range value fails startup loudly.
- `KB_PENDING_TIMEOUT_SEC`: age after which an unresolved pending proposal is
  auto-expired by the pending GC loop (default `86400`, i.e. 24h). Each expiry
  emits an audit event.
- `KB_SECRET_SCAN_EXTRA_PATTERNS`: comma-separated additional regexes the
  secret-scanning gate (issue #71) checks alongside its built-in pattern set
  (see "Write serialization and integrity gates" above). Each entry is scanned
  as its own named pattern (`custom_1`, `custom_2`, ...); an invalid regex, or
  one with the classic nested-quantifier ReDoS shape, is logged and skipped
  rather than raised, and every accepted pattern runs with a hard 1-second
  match timeout. Empty by default (no extra patterns).

## Read-only mirrors may scale horizontally

Set `KB_READ_ONLY=true` to run an instance as a read-only replica. In this mode
the server registers only the read tools (`kb_search`, `kb_get`, `kb_list`,
`kb_outline`, `kb_health`) and the read REST routes; the write and
enforcement-write tools and routes (`propose`, `resolve`, `bootstrap`,
`consult`, `gate`, `record-event`, and their observability mirrors) are not
registered at all and return 404. The write pipeline (worktrees, push queue,
pending) is never initialised, so no replica is a git writer.

Crucially, the `git_pull_loop` still runs, so each replica keeps `KB_REMOTE_URL`
set and refreshes its own index snapshot from the same git remote as the single
writer. Run as many replicas as you need for read throughput; the single
write-enabled instance remains the only owner of the git remote.

`KB_READ_ONLY` is a truthy flag: `1`, `true`, `yes`, or `on` (case-insensitive)
enable it; unset or anything else keeps the default read-write behaviour.

See `deploy/k8s/read-replica/` for a ready-to-apply `Deployment` (not the
StatefulSet writer) that runs N read replicas with per-pod ephemeral clone +
index scratch. Apply the base stack first, then the overlay:

```bash
kubectl apply -k deploy/k8s              # namespace + secret + writer StatefulSet
kubectl apply -k deploy/k8s/read-replica # N read replicas + read Service
kubectl -n data-olympus scale deployment/data-olympus-mcp-read --replicas=5
```

## Git pull loop

On startup, and at the interval set by `KB_SYNC_INTERVAL_SEC` (default 60s),
the server calls `git pull` on `KB_MAIN_PATH`. If `KB_REMOTE_URL` is empty,
the pull loop runs but exits cleanly with no action. The `health` endpoint
reports `degraded: true` only when the index has not been rebuilt within
`KB_STALENESS_DEGRADED_SEC` (default 600s).

For a local read-only demo with no remote, set `KB_REMOTE_URL=""`. The server
stays healthy as long as the index builds successfully on startup.

## Health, readiness, and liveness (three distinct signals)

The server exposes three endpoints that answer three different questions. They
are deliberately split so a data-freshness problem does not eject a pod that can
still serve reads:

| Endpoint | Question | 200 when | 503 when |
|---|---|---|---|
| `GET /api/v1/health` | Is the data fresh? | index built, fresh, last build ok | **degraded**: staleness > `KB_STALENESS_DEGRADED_SEC`, no successful pull, empty index, or last build failed |
| `GET /readyz` | Can this pod serve reads now? | process up **and** index loaded **and** last build ok | no index built yet, or the last index build failed |
| `GET /livez` | Is the process responsive? | always (if the handler runs, the loop is alive) | never |

Key point: **`/readyz` is independent of data staleness.** A git-remote outage
makes the KB stale (so `/api/v1/health` returns 503 with `degraded: true`), but
the last-good index still answers reads perfectly, so `/readyz` stays 200 and the
pod stays in the Service. The Kubernetes **readiness probe targets `/readyz`**
(see `deploy/k8s/statefulset.yaml`); if it targeted `/api/v1/health` a stale
single-replica pod would be ejected from the Service and a "reads are a bit old"
condition would become a hard 503 with zero endpoints.

`/api/v1/health` keeps its 503-on-degraded contract because the `bin/kb` CLI
`--no-stale` flag relies on it (exit 2 when the endpoint reports `degraded: true`,
whether over HTTP 200 or 503). **Alert on `/api/v1/health` degraded** (and on the
`last_git_fetch_status` / `staleness_seconds` fields it carries) rather than
wiring it to a probe. The liveness probe is a `tcpSocket` check by default; the
HTTP `/livez` route is offered for operators who prefer an explicit route. See
`docs/operations.md` for the full alerting and recovery model.

The health payload also carries `malformed_frontmatter`: the count of docs whose
front-matter was present but malformed at the last index build. A non-zero value
means a doc silently lost its governance metadata (`type`/`status`/`tier`), so it
will not be governed or filtered correctly. It is a **warning** signal and does
**not** flip `degraded` (that would 503 every read for an authoring mistake);
alert on `malformed_frontmatter > 0` separately.

## Write serialization and integrity gates

Every write (`kb_propose_memory`, `kb_propose_edit`, `kb_resolve_pending`, and
onboarding bootstrap — which commits its whole file bundle through the same
serialized/validated multi-file path) goes through one serialized critical
section so concurrent writes cannot corrupt each other:

- A **process-wide write serializer** wraps the write → `git add` → commit →
  enqueue sequence, so one thread's commit can never sweep another thread's staged
  file. Write volume is rate-limited, so a single global mutex is cheap.
- A **per-path advisory lock**, shared between the auto-commit path and the
  pending queue, prevents two writes to the same file from racing and prevents an
  auto-commit from landing on a path with a pending proposal in flight (whose
  later approval would clobber it). A write blocked by the lock returns
  `rejected_path_lock_busy`. An orphaned lock (a crash between lock acquisition
  and the pending entry write) is reclaimed by the pending GC loop.
- A **content-validation gate** runs on the postimage before commit. It rejects
  `rejected_invalid_document` when the frontmatter is malformed YAML, a
  `type`/`status`/`tier` enum value is out of vocabulary, or the `id` is a
  forged/duplicate of one already used by a different path (which would break
  every subsequent index rebuild — one bad write, persistent degraded state). The
  response `reason` carries the machine-readable errors.
- **Compare-and-swap (optimistic concurrency).** When a caller supplies a base
  marker (`base_commit` naming a specific commit, `base_blob_sha`, or
  `target_file_hash`) on `kb_propose_edit` (or it is carried on the pending entry
  into `kb_resolve_pending`), the server refreshes the session worktree's base
  onto `origin/main` and compares the marker against the current target content. A
  mismatch returns `rejected_stale_base` and nothing is committed. If the base
  cannot be refreshed at all (the remote is unreachable, or the rebase conflicts)
  while an enforceable marker was supplied, the write is also rejected
  `rejected_stale_base` rather than committed against a possibly-stale base — the
  marker cannot be verified, and the push-path rebase recovery is not an
  equivalent safety net (a compatible rebase would still publish the stale write).
  A bare `base_commit` of `HEAD` is advisory (no per-file expectation), and when
  no marker is supplied the pre-0.3.0 behavior is preserved (a refresh failure is
  non-fatal; the push path's non-FF recovery publishes the commit).
- Ordering of side effects: the commit message and all gates are evaluated
  BEFORE the file is written and `git add`-ed, and on any failure after the add
  the worktree is hard-reset, so a rejected write never leaves a staged leftover
  for the session's next commit to sweep in.
- A **secret-scanning gate** (issue #71) runs on the postimage BEFORE the
  content-validation gate, on every commit path (auto-commit propose, resolve
  approve, including a resolved `edited_text`, and onboarding bootstrap). It
  runs first so a postimage that is both malformed AND carries a
  credential-shaped value is always rejected via the redacted
  `rejected_secret_detected` path, never via `rejected_invalid_document`
  (which echoes the offending value verbatim in its message). The gate
  checks a built-in pattern set (PEM private-key blocks; GitHub `ghp_`/`gho_`/
  `ghs_`/`ghr_`/`github_pat_` tokens; AWS `AKIA...` access key ids; Slack
  `xox[bpars]-` tokens; generic `password=`/`passwd=`/`secret=` assignments,
  including env-style prefixed keys like `DB_PASSWORD=`, with a
  non-placeholder value; and `scheme://user:pass@host` connection strings)
  plus any operator-supplied `KB_SECRET_SCAN_EXTRA_PATTERNS`. A match on an
  auto-commit or bootstrap path rejects the write `rejected_secret_detected`
  before anything is written to disk. Only the pattern name and an
  approximate line number are ever surfaced in the response, the audit
  event, or a log line, never the matched value. A low-confidence proposal
  containing a secret still enters pending (removing it would defeat the
  operator-override workflow below), but the scan runs at propose time too:
  the response never echoes the raw text when flagged (only the pattern
  name), the pending entry is tagged `secret_scan_flagged`/`matching_pattern`
  (names only) so `kb pending` surfaces the warning, and a flagged memory
  proposal's filename falls back to a neutral slug instead of embedding the
  flagged text. The gate also covers the fields AROUND the postimage: a
  credential-shaped `target_path` (filename) is rejected on edit, bootstrap,
  and resolve without ever echoing the path back (it would otherwise land in
  responses, audit events, commit subjects, and the git tree); a flagged edit
  `reason` is replaced with a redacted note before it reaches pending meta,
  push metadata, or audit events; and a flagged memory tag is stored redacted
  in pending meta. `kb_resolve_pending` accepts an operator-only
  `override_secret_scan` boolean (`kb resolve --override-secret-scan` on the
  CLI) to consciously commit a false positive anyway (recorded in the audit
  event); no auto-commit or bootstrap path exposes this override, so an agent
  can never self-authorize past a flagged write. An extra pattern with the
  classic nested-quantifier ReDoS shape is rejected at load time alongside an
  invalid regex, and every accepted custom pattern executes through the
  `regex` engine with a hard 1-second match timeout, so a catastrophic
  pattern the load-time check misses is bounded at scan time (logged and
  skipped) instead of hanging the single-writer write path.

## Push queue and write-path visibility

Committed writes are published to `origin/main` through a durable push queue: a
commit that returns "committed" has a queue entry on disk, and a background loop
drains the queue with retry. Health surfaces the queue state so a stuck write
path is diagnosable:

- `push_queue_size` is the live count of queued (not-yet-pushed) entries.
- `pending_count` is the live count of pending (awaiting-approval) entries.
- `push_queue_frozen` is the count of entries that hit the retry cap and were
  **frozen**.

Both counts are computed at health-report time from the queues themselves, so
they cannot drift.

### Non-fast-forward push recovery

When a second overlapping session moves `origin/main` between a session's commit
and its push, the push is rejected **non-fast-forward**. The push loop classifies
this distinctly from a network failure: it fetches `origin/main`, rebases the
session branch onto it, and retries the push once. Most non-FF cases (the two
sessions touched different files) publish cleanly on the retry.

If the rebase **conflicts** (the two sessions edited the same lines
incompatibly), the commit cannot be auto-published. Instead of retrying forever,
the push loop **demotes** the commit to a pending entry for operator resolution:
the postimage is re-proposed as a pending edit (visible via `kb_list_pending`),
the queue entry is removed, and a `push_conflict_demoted` audit event is recorded.
The operator resolves the pending entry (approving re-applies it on the current
base, subject to the CAS/validation gates) to publish the change.

Pure **contention** (the retry push is itself non-FF because `origin/main` moved
again mid-rebase) is retried in-line for a bounded number of passes; if it never
wins the race it is demoted the same way rather than counted toward the freeze
cap, so persistent contention never becomes a silently-frozen queue item.

### Startup recovery

A crash in the narrow window between `git commit` and the push-queue enqueue
would otherwise orphan a commit on a session worktree branch that nothing pushes.
On boot the server scans every session worktree and re-enqueues any commit
reachable from its `HEAD` but not from `origin/main`. Entries already in the
queue are left untouched, so recovery never double-enqueues. The recovery result
is logged (`startup push recovery re-enqueued N orphaned commit(s)` or `no
orphaned commits found`).

### Frozen entries and how to unfreeze

After `max_attempts` consecutive push failures a queue entry is **frozen**: the
retry loop stops retrying it (retrying a permanently failing push every interval
only spams the remote and hides the problem). The freeze is logged once at WARN
with the sha, worktree, and last error, and the entry is counted in
`push_queue_frozen`. **A nonzero `push_queue_frozen` means writes are stuck and
need operator attention.** Non-fast-forward pushes against a moved `origin/main`
are now recovered automatically (see *Non-fast-forward push recovery* above), so
a frozen entry now signals a persistent failure the rebase path could not
handle — typically an authentication, network, or remote-side rejection.

To unfreeze, an operator resolves the underlying push failure, then clears the
entry file directly on the push-queue volume (`KB_PUSH_QUEUE_ROOT`, default
`/state/push-queue`):

- **Requeue** (retry from scratch): delete the `"frozen": true` and reset
  `"attempts"` to `0` in the entry's JSON file (or delete and let startup
  recovery re-enqueue it), then the retry loop picks it up again on its next
  pass.
- **Drop** (abandon the write): delete the entry's `<sha>.json` file. The commit
  stays on its session worktree branch but is never published.

There is no unfreeze API in this release; the file-level procedure above is the
supported path.

## Per-session worktree GC

Each writing session gets its own git worktree (and a `kb-session/<safe_id>`
branch) so in-flight edits are isolated. A background GC task, running every 5
minutes, removes worktrees idle beyond `KB_WORKTREE_IDLE_SEC` (default 3600s) so
KB checkouts do not accumulate one-per-session forever.

GC is conservative and coupled to the push queue:

- A worktree with commits **not yet reachable from `origin/main`** is deferred
  (never removed) until the push queue drains those commits; the next GC pass
  cleans it up once they land upstream.
- When a worktree is removed, its `kb-session/<safe_id>` branch is deleted in the
  same step. This matters because a returning session recreates its worktree with
  `git worktree add -b kb-session/<safe_id>`, which would fail if the branch
  still existed. Deleting the branch keeps a GC'd session able to write again.

## Streamable-http session lifecycle and reaping

Each session-less `POST /mcp` handshake makes the underlying MCP SDK create a
transport, register it in an in-memory session table, and start a task that
blocks waiting for further messages on that session. The SDK removes a session
only on an explicit client `DELETE`, on the session task crashing, or on server
shutdown. It also supports an idle timeout, but only when its session manager is
constructed with one, and FastMCP does not wire one through: under its default
construction the idle timeout is unset, so nothing reaps idle sessions.

Consequence: a client that handshakes and drops the connection without sending
`DELETE` (the common cause of repeated "Created new transport with session ID"
log lines) leaves its transport resident. Over long uptime the session table
grows without bound and leaks memory and tasks.

data-olympus closes this two ways:

- Observability: `health` reports `live_sessions`, the current live transport
  count (or `null` before the HTTP app has started serving). The server also
  logs the live count each reaper pass. A `live_sessions` value that only ever
  climbs is the signal of a leak.
- Bound: a background reaper terminates sessions idle beyond
  `KB_SESSION_IDLE_TIMEOUT_SEC` (default 1800s / 30 min). It scans every
  `KB_SESSION_REAP_INTERVAL_SEC` (default 60s). Set
  `KB_SESSION_IDLE_TIMEOUT_SEC=0` to disable reaping and keep observability
  only. Termination uses the SDK's own `terminate()` path, so a client that
  reconnects simply gets a fresh session.

Idle is measured from the last *request* seen for a session: the activity clock
advances only when a request carrying that session's `mcp-session-id` header
reaches the server. A client that keeps polling or making periodic calls is
therefore never reaped, because each call re-stamps its activity.

The consequence to be aware of is that "idle" is per-request, not
per-connection. A quiet long-lived `GET` SSE stream that stays open but makes no
periodic `POST` requests still stamps no activity, so after
`KB_SESSION_IDLE_TIMEOUT_SEC` its session is reaped and the stream is torn down;
the client must reconnect (it gets a fresh session on the next handshake). If
your client relies on a long-lived stream without periodic requests, either
raise `KB_SESSION_IDLE_TIMEOUT_SEC` above your longest expected quiet period,
set it to `0` to disable reaping (observability only), or have the client send a
periodic keep-alive request. Excluding sessions with an active open stream from
reaping (so a live stream is never torn down) is a possible follow-up; the
current behavior reaps purely on request-activity age.
## Search ranking

`kb_search` orders hits by BM25 relevance and then applies a **status-aware
rerank on top of that score, enabled by default**. The rerank exists so a
retired document does not outrank the live one that replaced it: for a query that
matches both, the in-force doc is promoted and the retired one demoted.

The built-in status-to-weight map is:

- In-force (boosted): `active`, `accepted`, `approved`.
- Retired (penalized): `superseded`, `deprecated`, `rejected`.
- Not yet in force (penalized): `draft`, `proposed`.

A negative weight boosts (moves a hit earlier), a positive weight penalizes
(moves it later). Status matching is case-insensitive, so `Active` in frontmatter
ranks like `active`. Any status not in the map (including an empty status) is
neutral and is never dropped from results.

Because this reorders results, a query that previously surfaced a `superseded`
doc first may now surface the `active` one first. This is the intended behavior
change.

Override the map at deploy time, with no code change:

- `KB_STATUS_WEIGHTS`: a JSON object of `{status: weight}` that **replaces** the
  built-in map (it is not merged). Keys are statuses (matched
  case-insensitively), values are numeric deltas: negative boosts an in-force
  status, positive penalizes a retired one. A malformed value fails startup
  loudly rather than silently shipping default ranking. Unset (the default) uses
  the built-in map above.

  Example: `KB_STATUS_WEIGHTS='{"active": -1.0, "approved": -1.0, "superseded":
  2.0, "draft": 1.0}'`.

## `in_force`: hard in-force filter

`kb_search` accepts an `in_force: bool = False` parameter (MCP tool and
`GET /api/v1/search?in_force=true`). When set, results are HARD-filtered
BEFORE ranking to docs that are currently in force: the in-force status class
(`active`, `accepted`, `approved`) AND the validity window
(`validity.valid_from` not in the future, `validity.valid_until` not in the
past; both boundary days inclusive). So a `superseded`, `deprecated`,
`draft`, `proposed`, expired, or upcoming doc is excluded entirely rather
than merely soft-downranked by the status rerank above.

Use it when you want only guidance that currently applies and a demoted-but-
present retired doc is not acceptable. It differs from the status rerank: the
rerank always keeps every hit and only reorders; `in_force` drops out-of-force
hits. The in-force predicate is defined once
(`format.validate.IN_FORCE_STATUSES` for the status class,
`format.validate.is_in_force` for the combined status-AND-window predicate)
and shared by the rerank boosts and this filter.

`in_force` composes with the single-status `status` filter: passing both
requires a doc match `status` AND be in-force (so `status=superseded` with
`in_force=true` yields nothing). The filter also applies to the dense
(embedding) candidate source, so a hybrid deployment never leaks an out-of-force
doc through the semantic path.

The predicate also applies a memory-inbox floor (issue #109): a document
under the memory-inbox prefix (`KB_MEMORY_INBOX_PREFIX`, default
`memory/inbox/`) is never in force regardless of claimed status, so a legacy
inbox file or forged frontmatter on an agent-written memory cannot satisfy
`in_force=true` no matter what `status` it declares. This is a plain `is_inbox`
column derived once at index build time, not a per-query prefix scan. Together
with the supersession-graph exclusion below, the full in-force predicate is:
status class AND validity window AND not-inbox AND not-graph-excluded.

`kb_consult` passes `in_force=true` on its internal retrieval unconditionally
(not a caller-facing parameter): the enforcement surface must never present an
unreviewed, retired, expired/upcoming, memory-inbox, or graph-excluded
document as a governing rule for a code/architectural decision. See
`docs/enforcement.md`.

**Computed `in_force` on served responses.** A verbose (`verbose=true`)
`kb_get` response and each verbose `kb_search` hit carry a computed
`in_force: bool`: the same single-sourced predicate evaluated against the
doc's actual status/validity/inbox-membership/graph-exclusion, independent of
whether the query itself passed `in_force=true`. This lets a caller retrieve
a doc via a default (unfiltered) `kb_get`/`kb_search` and still tell whether
it currently governs, without a second `in_force=true` round trip. It is a
serving-layer derivation only, never written to frontmatter (see SPEC.md's
"runtime envelope" note). Compact responses emit it deviation-only:
`in_force: false` appears ONLY when the doc is not in force, and an in-force
doc's compact shape is byte-for-byte unchanged. The deviation emission is
required because the compact `status`/`freshness` fields key off the RAW
frontmatter status: a memory-inbox doc with a forged `status: active` would
otherwise render as an ordinary current rule with no signal that the in-force
floor disqualified it.

## Supersession-graph exclusion (issue #110 slice 2)

`in_force=true` ALSO excludes any document that is the TARGET of a
`supersedes` edge whose SOURCE document is itself in force (the full
status-class-AND-validity-window predicate above, not merely
`status: superseded`). This is the in-force-source guard: a `draft`, expired,
or already-retired document can never retire another document just by naming
it in `supersedes`. It closes the "forgotten status flip" gap -- A (active)
supersedes B, but B's own `status` was never updated to `superseded` -- B is
excluded from `in_force=true` results even though its own status class would
otherwise qualify it. Like the `upcoming` half of the validity window, graph
exclusion is scoped to `in_force=true` only: a plain (default) search still
returns the excluded document.

A mutually-supersessive in-force cycle (already a `kb lint` ERROR at parse
time) is NOT special-cased at retrieval time: every member independently
satisfies the exclusion rule (each is the target of an in-force source's
`supersedes` edge), so `in_force=true` excludes ALL of them. A dangling edge
(source or target id with no corresponding document) excludes nothing; the
exclusion query joins `edges` to `docs` on both ends.

`kb_consult` runs its search with `in_force=true` (see "Enforcement
endpoints" below), so it never returns a graph-excluded, not-yet-in-force, or
retired document -- the same guarantee the unconditional expired-doc exclusion
already gave it, made explicit for the graph rule since that rule is scoped to
`in_force=true` rather than applying unconditionally.

A `graph_excluded_docs` health counter (in `kb_health` / `/api/v1/health`,
alongside `malformed_frontmatter` and `malformed_validity`) reports the count
of documents currently excluded by this rule. It is evaluated LIVE at
health-read time against the current date, from the same SQL definition the
retrieval-time filter uses (`format.validate.graph_excluded_ids_sql`), so
the counter can never drift from the filter it reports on -- including
across a date boundary where an in-force source's validity window opens or
closes between index rebuilds (retrieval evaluates the window per query, so
a counter frozen at build time would keep reporting a stale value). The
short health cache (`health_ttl_sec`, default 5s) bounds its staleness. It
is a WARNING signal only and does not flip `degraded`.

### Retirement is explainable

`kb_get` by id always resolves regardless of in-force or graph-exclusion
status (same as it already ignores expiry) and returns:

- `superseded_by`: the sorted, deduped UNION of the document's own
  frontmatter `superseded_by` claim and any reverse `supersedes` edge naming
  it. This ONE consistent computed shape covers both the honest self-declared
  case and the forgotten-status-flip case above, where only the superseding
  document's own `supersedes` list names this one.
- `contradicts`: the document's own frontmatter list (unchanged from the
  frontmatter, never affects filtering or ranking anywhere in the retrieval
  path).
- `contradicted_by`: the computed reverse -- every other document whose
  `contradicts` names this one.

All three are omitted from the compact `kb_get` response when empty (same
deviation-only pattern as `freshness`). Compact `kb_search` hits carry a
deviation-only `superseded_by` (the same computed union above), omitted when
the document is not superseded; it is computed and attached to EVERY hit
regardless of `in_force`, so a plain search result still explains why a hit is
historically superseded. `kb_search` hits do NOT carry `contradicts` /
`contradicted_by`; those are kb_get-only.

## Validity: expired docs leave default results

Independent of `in_force`, a doc past its `validity.valid_until` date is
excluded from EVERY default `kb_search` result (SPEC.md section 4.2): an
expired doc has no named successor to outrank it, so left visible it could be
the top hit and would govern. Three related `kb_search` parameters (MCP tool
and `GET /api/v1/search`):

- `include_expired: bool = false` restores expired docs to the result set;
  each carries `freshness: "expired"` in its hit.
- `validity_state` is an audit facet: `"expired"`, `"stale"` (past
  `recheck_by`), or `"expiring_within:N"` (docs whose `valid_until` falls
  within N days). Filtering for `"expired"` implies including expired docs. A
  malformed value is rejected (HTTP 400 on REST).
- Compact hits carry a deviation-only `freshness` field
  (`stale`/`expired`/`upcoming`), omitted when fresh or when the doc has no
  `validity` block. A doc with a future `valid_from` stays in default results
  flagged `upcoming`; only `in_force=true` excludes it. A stale doc (past
  `recheck_by`) stays in force and visible.

`kb_get` by id always resolves regardless of expiry, returning the full
`validity` object plus the computed `freshness` indicator. `kb_consult` never
returns an expired (or upcoming, proposed/retired, memory-inbox, or
graph-excluded) doc, via its unconditional `in_force=true` retrieval (see the
`in_force` and "Supersession-graph exclusion" sections above), not merely by
reusing the default search path's own expired-exclusion.
The `data-olympus validity-report` CLI subcommand lists expired and
soon-to-expire docs from a bundle directory.

## `abstain`: signal-gated abstention

`kb_search` accepts an `abstain: bool = False` parameter (MCP tool and
`GET /api/v1/search?abstain=true`). When set, the query is first run restricted
to the discriminating columns (`title`, `tags`, `applies_when`). If it matches
none of them, the query is treated as out-of-scope and the search returns NO
hits with `abstained: true` and `abstain_reason: "no_signal_match"`, instead of
surfacing a weak match on generic body prose. A query with a real signal
retrieves normally over all columns (recall is preserved).

The response distinguishes a deliberate abstention from an ordinary empty
result: a normal search that simply finds nothing returns `abstained: false`.
The gate logic is single-sourced in `data_olympus.search_gate`; the benchmark
ablation imports it rather than reimplementing it.

## Taxonomy and writable paths

The server maps each document's path to a `(tier, category)` pair. The built-in
default is deployment-neutral and covers `universal/`, `tech-stacks/<stack>/`,
`decisions/`, `workflows/`, `memory/` (with `memory/inbox/` and
`memory/accepted/`), `tooling/`, `templates/`, and `projects/<name>/`
(with `components/<component>/` for T4).

A bundle that uses a different directory layout overrides the defaults at deploy
time, with no code change:

- `KB_TAXONOMY_PATH`: path to a JSON file holding a list of
  `[prefix, tier, category]` triples that **replaces** the default table. The
  `tech-stacks/` and `projects/` prefixes keep their dynamic `stack:<name>` /
  `project:<name>` behavior if present.
- `KB_INDEXED_PREFIXES`: comma-separated list of writable top-level prefixes
  that **replaces** the default writable set. Writes outside these prefixes are
  rejected by the structural rule.
- `KB_MEMORY_INBOX_PREFIX`: directory new memory proposals are written under
  (default `memory/inbox/`).

## Synonym / acronym query expansion

Before building the FTS MATCH, the server rewrites the query term list through a
curated, bidirectional synonym map. A short-form query (`k8s`, `rls`) also reaches
documents that only use the long form (`kubernetes`, `row level security`), and
vice versa; adjacent query tokens are scanned as n-grams so multi-word keys match
from a long-form query. Expansion is bounded (32 terms) and de-duplicated, and the
terms the user actually typed are ranked first. This is curated lexical expansion,
not semantic (embedding) retrieval.

The default map ships a small set of high-value, unambiguous technical acronyms
(`k8s`/`kubernetes`, `authn`/`authz`, `rls`, `adr`, `rbac`, `sso`, `pii`, `iac`,
`kb`). Ambiguous or generic short tokens are intentionally left out of the
defaults to avoid recall noise; add them per deployment via `KB_SYNONYMS`.

Two environment variables tune it:

- `KB_SYNONYMS`: extra or override groups in `key=variant,variant;key2=variant`
  form. Groups are `;`-separated; within a group the `key` is the canonical form
  and the comma-separated tail are its variants. Whitespace is trimmed and empty
  entries are dropped. Multi-word keys and variants are allowed (e.g.
  `row level security=rls`). Example:
  `KB_SYNONYMS="feature flag=ff,flag;service level objective=slo"`.
- `KB_SYNONYMS_MODE`: how `KB_SYNONYMS` combines with the built-in defaults.
  - `merge` (default): layer `KB_SYNONYMS` on top of the curated defaults; a key
    present in both is overridden by `KB_SYNONYMS`.
  - `replace`: use only `KB_SYNONYMS`; the curated defaults are dropped.
  - `off`: disable expansion entirely (the expander becomes a passthrough, so
    only the terms the user typed are matched).

## Optional local-embedding hybrid ranking

For real paraphrase / synonymy handling (a query that shares no tokens with the
relevant doc, e.g. `car` for a doc about `automobile`), the server can blend BM25
with LOCAL embedding similarity. This is **optional and OFF by default**: with
the feature off and the `embeddings` extra uninstalled, nothing imports the
embedding stack and the default lexical product is unchanged. There is **no
external API and no network call at query time**; a small ONNX model runs
in-process.

When enabled, each document is embedded at index-build time and its vector is
stored in a `doc_vectors` table (schema v8) written into the same tmp database
and swapped atomically with the rest of the index, so vectors rebuild
atomically. At query time a hybrid reranker embeds the query once and blends
**normalised BM25** with **cosine similarity** over the candidate hits' stored
vectors, then re-sorts. It composes UNDER the exact-id / exact-tag short-circuit
(so an exact id or tag still ranks first) and wraps the status-aware rerank (so
an active doc still outranks the superseded one it replaced). When disabled the
reranker stack is exactly the status + id/tag behaviour described under **Search
ranking**.

Enable and tune it with:

- `KB_EMBEDDINGS_MODE`: `on` to enable; any other value (including unset) leaves
  it off.
- `KB_EMBEDDINGS_MODEL`: the local model name (default `BAAI/bge-small-en-v1.5`,
  a 384-dimensional MiniLM-class model). Any `fastembed`-supported model works.
- `KB_EMBEDDINGS_WEIGHT`: the cosine fraction of the blended score, in `[0, 1]`
  (default `0.35`). `0.0` is pure lexical (BM25) ordering, `1.0` is pure
  semantic. A malformed or out-of-range value fails startup loudly.

Install the extra to make the model runner available:

```bash
uv sync --extra embeddings   # or: pip install 'data-olympus[embeddings]'
```

The extra pulls in `fastembed`, which bundles `onnxruntime` (there is no torch
dependency). The default model ships as a ~33 MB quantised ONNX file that is
fetched once when the feature is first enabled and cached locally; after that
there is no network access. If `KB_EMBEDDINGS_MODE=on` but the extra or the model
is unavailable, **startup fails loudly** with an actionable message rather than
silently reverting to lexical-only ranking.

Cost: build adds one batched local embedding pass over the corpus (seconds for a
small KB); storage adds `dim x 4` bytes per document in `doc_vectors` (~1.5 KB
per doc for the 384-dim default). Query-time cost is one query embedding plus a
cosine over the (bounded) candidate pool.

## Corpus co-occurrence query expansion

A build-time pass mines the corpus for term pairs that co-occur far more often
than chance (positive pointwise mutual information) and stores the top related
terms per term. At query time the expander adds those related terms so a query
reaches documents that use a different-but-associated vocabulary, without a
hand-curated synonym map. This is **ON by default** and adds no query-time
network call. It auto-disables on a corpus below the doc floor (too little
signal), and the O(n^2) pair counting is bounded per document.

Tune it with:

- `KB_COOCCURRENCE_MODE`: `off` disables it (both the build-time table and the
  query-time expansion); any other value (including unset) leaves it **on**.
- `KB_COOCCURRENCE_K`: max related terms kept per term (default `5`).
- `KB_COOCCURRENCE_MIN_COUNT`: minimum raw co-occurrence count for a pair to
  qualify (default `3`).
- `KB_COOCCURRENCE_MIN_PMI`: minimum PMI for a pair to qualify (default `0.1`).
- `KB_COOCCURRENCE_MIN_DOCS`: corpus-size floor below which co-occurrence is
  auto-disabled (default `50`).
- `KB_COOCCURRENCE_MAX_DOC_TOKENS`: per-document unique-token cap on the pair
  counting, bounding the O(n^2) work on large docs (default `400`).

## Trigram fuzzy-match fallback

For typos and partial identifiers, an optional trigram index backfills results
when the primary FTS query returns few hits. This is **OFF by default** so the
default deployment pays no trigram build or storage cost; when off, the trigram
table is neither created nor populated. When on, a primary query returning at or
below the threshold is backfilled from the trigram index, and the backfill only
ever appends after the primary hits (never reorders them).

Tune it with:

- `KB_TRIGRAM_MODE`: `on` (case-insensitive) enables it; any other value
  (including unset) leaves it off.
- `KB_TRIGRAM_FALLBACK_THRESHOLD`: primary-hit count at or below which the
  fallback fires (default `3`). A malformed or negative value falls back to the
  default.

## Authentication and network security

The server supports optional bearer-token authentication with a per-principal
capability model, enforced on **both** the REST routes and the MCP write tools:

- `KB_AUTH_TOKEN` registers a single full-capability `operator` principal.
- `KB_AUTH_PRINCIPALS` (JSON) registers per-agent tokens with explicit
  capabilities; a principal lacking `auto_commit` has its proposals clamped to
  pending regardless of the client-asserted confidence.

When auth is configured, write, enforcement, and observability routes require a
capable principal; read routes (`search`/`get`/`list`/`outline`/`health`) stay
open. When it is unset (the default) every caller is fully trusted, which is only
safe on a trusted private network.

Authentication does not protect read routes, so for confidentiality operators
MUST still deploy on a trusted private network or behind an authenticating
reverse proxy (terminate TLS there). See `SECURITY.md` for the full threat model,
the route/capability table, payload limits, the tamper-evident audit log, and the
git sync-failure health fields.

### Proxy headers and the rate limiter (`KB_TRUSTED_PROXIES`)

The rate limiter keys on the client remote address (plus principal). Behind an
ingress or reverse proxy, uvicorn by default sees the **proxy's** address as the
peer, so every client collapses into one `remote_addr` and the per-IP cap
(`KB_RATE_LIMIT_PER_IP_PER_HOUR`) throttles all clients as if they were one.

Set `KB_TRUSTED_PROXIES` to the proxy address(es) (comma-separated) to fix this:

- **Unset (default)** — uvicorn runs with `proxy_headers` **off**. `X-Forwarded-For`
  is ignored and `remote_addr` is the immediate peer. This is the safe default: a
  direct client cannot spoof its address to dodge the limiter.
- **Set to the proxy IP(s)** — uvicorn enables `proxy_headers` and restricts
  `forwarded_allow_ips` to those addresses, so it rewrites `remote_addr` from
  `X-Forwarded-For` **only** when the immediate peer is a trusted proxy. The
  limiter then sees the true client IP.
- **`*`** — trust `X-Forwarded-For` from any peer. Only safe when nothing
  untrusted can reach the port directly (e.g. the port is bound to the pod network
  and only the ingress can connect). Otherwise a direct client could forge the
  header.

### The gate-check route (`KB_GATE_CHECK_RATE_LIMIT_PER_HOUR`)

`POST /api/v1/gate/check` (and the `kb_gate_check` MCP tool) is the enforcement
hook's freshness probe, called **once per gated tool action** by every agent. It
therefore must not share the write/consult quota (`KB_RATE_LIMIT_PER_HOUR`): with
several agents active, and all clients collapsing to one limiter bucket behind an
ingress, a fixed hourly quota self-throttles the whole fleet with `429`s. So
gate-check has its own ceiling:

- **`KB_GATE_CHECK_RATE_LIMIT_PER_HOUR=0` (default)** — gate-check is **not**
  rate-limited. It does only cheap classification plus a freshness lookup and no
  writes, so this matches the read routes, which are also unthrottled.
- **A positive value** — an explicit per-(address, principal) backstop just for
  gate-check, independent of the write/consult limiter. Size it above your fleet's
  real per-hour tool-call volume, not near it, or you reintroduce the self-DoS.

The `consult` and `onboarding/cleanup-plan` routes remain throttled by
`KB_RATE_LIMIT_PER_HOUR` (they write to the ledger or are CPU-heavy).

## Audit-log rotation (`KB_AUDIT_MAX_BYTES`)

The JSONL audit log (`KB_AUDIT_LOG_PATH`, default `/state/audit/events.log`) grows
without bound by default. Set `KB_AUDIT_MAX_BYTES` to a byte threshold to enable
size-based rotation: when the live file passes the threshold, the next append
first renames it to `events-<UTC-timestamp>.log` and starts a fresh live file.

- **Chain continuity.** The tamper-evident hash chain carries **across** the
  rotation boundary: the first event of the new file links to the last hash of the
  rotated file, so `GET /api/v1/audit/verify` (and the `kb audit`/`verify` logic)
  validates the whole history, rotated segments included, in chronological order.
  A break at the boundary is caught like any other.
- **Backward compatible.** With `KB_AUDIT_MAX_BYTES` unset (0), nothing rotates:
  the log stays a single file exactly as before, and an existing single-file log
  still verifies unchanged when opened by a rotation-aware build.
- **Reads.** `kb audit` reads the live file by default (cheap, most-recent-first).
  A `--since` query also walks rotated segments so history that has rotated out of
  the live file is still visible; the reverse scan is bounded so a large archive
  cannot turn one query into an unbounded read.

Manual `mv`-based rotation (documented in the operator runbook) no longer breaks
the chain when the process keeps running, because the in-memory last-hash carries
forward; but prefer `KB_AUDIT_MAX_BYTES` so rotation is automatic and the boundary
link is always written. See `docs/operations.md` for backup/verify procedures.

## Git commit identity

Every write commits to the KB repo, so git needs an author/committer identity.
The shipped Docker image sets a default (`data-olympus-mcp <data-olympus-mcp@localhost>`)
in `deploy/docker/entrypoint.sh` so the artifact can commit out of the box; the
server's `main()` also fills unset `GIT_*` variables as a belt-and-suspenders for
non-Docker runs. Override the identity with:

- `KB_GIT_AUTHOR_NAME` — commit author/committer name (default `data-olympus-mcp`).
- `KB_GIT_AUTHOR_EMAIL` — commit author/committer email (default
  `data-olympus-mcp@localhost`).

A pre-existing `GIT_AUTHOR_*`/`GIT_COMMITTER_*` value or a real `git config`
identity is never overwritten.

## Running locally

See `docs/quickstart.md` for the verified local-run procedure using
`scripts/run-local.sh`.

## Running in Kubernetes

See `deploy/k8s/` for the kustomize manifests. Apply them with:

```bash
# Apply namespace and non-secret resources
kubectl apply -k deploy/k8s/

# Apply the encrypted secret separately (operators use SOPS)
sops exec-file deploy/k8s/secret.sops.yaml 'kubectl apply -f {}'
```

The `deploy/k8s/secret.template.yaml` file shows the expected secret shape.
Fill in your git remote URL and deploy key, then encrypt with SOPS before
committing. Never commit the unencrypted secret.

The default `kubectl apply -k deploy/k8s/` deliberately does **not** create the
Ingress: it publishes all routes (including the unauthenticated-by-default write
routes) to the LAN. The Ingress is opt-in and enabled only after you set
`KB_AUTH_TOKEN`; see `deploy/k8s/README.md` for the enablement steps.

The pod is fully rootless: both the `prepare-git` initContainer (which stages the
deploy key and does the first-boot `/kb-main` clone) and the main container run as
uid 65534 with `runAsNonRoot: true`, all capabilities dropped, and a read-only
root filesystem, so the manifest passes the **restricted** Pod Security Standard.
The base image is digest-pinned in `deploy/docker/Dockerfile`; the git SSH host is
build-arg + runtime-env configurable (`KB_SSH_KEYSCAN_HOST`) for non-GitHub
remotes. Operational procedures (backup, upgrade, recovery playbooks) live in
`docs/operations.md`.

## Running with Docker Compose

```bash
docker compose -f deploy/docker/compose.yaml up --build
```

See `docs/quickstart.md` section 5 for the bind-mount pattern to serve a
real bundle.
