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

## Write serialization and integrity gates

Every write (`kb_propose_memory`, `kb_propose_edit`, `kb_resolve_pending`,
onboarding bootstrap) goes through one serialized critical section so concurrent
writes cannot corrupt each other:

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
  marker (`base_commit`, `base_blob_sha`, or `target_file_hash`) on
  `kb_propose_edit` (or it is carried on the pending entry into
  `kb_resolve_pending`), the server refreshes the session worktree's base onto
  `origin/main` and compares the marker against the current target content. A
  mismatch returns `rejected_stale_base` and nothing is committed. When no marker
  is supplied, the pre-0.3.0 behavior is preserved.
- Ordering of side effects: the commit message and all gates are evaluated
  BEFORE the file is written and `git add`-ed, and on any failure after the add
  the worktree is hard-reset, so a rejected write never leaves a staged leftover
  for the session's next commit to sweep in.

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
`GET /api/v1/search?in_force=true`). When set, results are HARD-filtered to the
in-force status class (`active`, `accepted`, `approved`) BEFORE ranking, so a
`superseded`, `deprecated`, `draft`, or `proposed` doc is excluded entirely
rather than merely soft-downranked by the status rerank above.

Use it when you want only guidance that currently applies and a demoted-but-
present retired doc is not acceptable. It differs from the status rerank: the
rerank always keeps every hit and only reorders; `in_force` drops out-of-force
hits. The in-force class is defined once (`format.validate.IN_FORCE_STATUSES`)
and shared by both the rerank boosts and this filter.

`in_force` composes with the single-status `status` filter: passing both
requires a doc match `status` AND be in-force (so `status=superseded` with
`in_force=true` yields nothing). The filter also applies to the dense
(embedding) candidate source, so a hybrid deployment never leaks an out-of-force
doc through the semantic path.

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

## Running with Docker Compose

```bash
docker compose -f deploy/docker/compose.yaml up --build
```

See `docs/quickstart.md` section 5 for the bind-mount pattern to serve a
real bundle.
