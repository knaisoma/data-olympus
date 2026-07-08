# Operations runbook

Operational procedures for running data-olympus in production: backup, upgrade,
recovery playbooks, and the health/readiness/alerting model. For the architecture
of the serving path (write serialization, push queue, session lifecycle) see
`docs/serving.md`; for the security posture see `SECURITY.md`.

Throughout, the Kubernetes examples target a single-writer StatefulSet pod
`data-olympus-mcp-0` in namespace `data-olympus`. Adjust for your deployment.

---

## 1. Health, readiness, and alerting model

Three endpoints answer three different questions (full table in
`docs/serving.md`):

- **`GET /readyz`** — *Can this pod serve reads now?* 200 when the process is up
  and the index is loaded and the last build succeeded; 503 otherwise.
  **Independent of data staleness.** This is the Kubernetes readiness-probe
  target.
- **`GET /livez`** — *Is the process responsive?* Always 200 if it answers at all.
  (The default liveness probe is a `tcpSocket` check; `/livez` is the HTTP
  equivalent.)
- **`GET /api/v1/health`** — *Is the data fresh and the write path healthy?*
  Returns 503 with `degraded: true` when stale, when no pull has succeeded, when
  the index is empty, or when the last build failed. **This is the alerting
  surface, not a probe target.**

### What to alert on

Poll `GET /api/v1/health` from your monitoring system and alert on:

| Condition | Meaning | Severity |
|---|---|---|
| `degraded == true` | KB is stale or the last index build failed | page if sustained > a few minutes |
| `last_git_fetch_status in (fetch_failed, ff_failed)` | git remote unreachable or history diverged | investigate; see §4.1 |
| `staleness_seconds` climbing past `KB_STALENESS_DEGRADED_SEC` | pulls not landing | investigate; see §4.1 |
| `push_queue_frozen > 0` | writes stuck, need manual unfreeze | investigate; see §4.4 |
| `pending_count` growing unbounded | proposals not being resolved by an operator | review the pending queue |
| `last_index_build_status == failed` | a rebuild left the last-good index in place (e.g. duplicate id) | investigate `last_index_conflicts`; see §4.2 |
| `malformed_frontmatter > 0` | one or more docs silently lost governance metadata (`type`/`status`/`tier`) | fix the offending doc(s); **warning**, not a service failure |

Why `malformed_frontmatter` does NOT flip `degraded`: it is an authoring-quality
issue, not a serviceability failure. Flipping `degraded` would 503 every read (via
the CLI `--no-stale` contract) for one bad document. Alert on it separately and
fix the front-matter; the index still serves every well-formed doc.

Do **not** point the Kubernetes readiness probe at `/api/v1/health`. On a stale
KB it returns 503, which would eject the (single-replica) pod from the Service and
turn "reads are slightly old" into a hard outage with zero endpoints. The probe
targets `/readyz` for exactly this reason.

### Verify tamper-evidence of the audit chain

```bash
curl -s http://<host>/api/v1/audit/verify   # {"ok": true, "first_broken_index": -1}
```

`ok: false` means the hash chain is broken at `first_broken_index` (counted across
all rotated segments + the live file, chronologically). Investigate before trusting
recent audit records: someone edited, deleted, reordered, or (if `KB_AUDIT_HMAC_KEY`
is set) forged a record, or a rotated segment was hand-edited (see §2.2). Preserve
the current `/state/audit/` for forensics before any further writes; a clean backup
(§2.2) taken while `verify` was `ok` is the recovery baseline.

---

## 2. Backup

The KB **content** is a git repo, so `origin/main` is the primary backup: every
committed document is on the remote. But three pieces of state live only on the
pod's PVCs and are **not** covered by the git remote. Back these up separately.

### 2.1 What the git remote does NOT cover

1. **The audit chain** (`/state/audit/`) — the tamper-evident JSONL log and its
   rotated segments. Lost audit history cannot be reconstructed from git.
2. **Pending proposals** (`/state/pending/`) — low-confidence writes awaiting
   operator approval. These have not been committed, so they are not on the
   remote; losing the volume drops them.
3. **Unpushed worktree commits** (`/kb-worktrees/`, `/state/push-queue/`) —
   commits that were made on a session branch but not yet pushed (including frozen
   or demoted push-queue entries). Until they reach `origin/main` they exist only
   on the pod.

### 2.2 Backing up the audit chain

```bash
# Snapshot every audit segment (live + rotated) out of the pod.
kubectl -n data-olympus exec data-olympus-mcp-0 -- \
    tar -czf - -C /state audit > audit-$(date +%Y%m%d).tar.gz

# Verify integrity of the running chain first, so you know the snapshot is clean:
curl -s http://<host>/api/v1/audit/verify
```

Because rotation carries the hash chain across files (see `docs/serving.md`), a
backup that includes **all** `events*.log` files verifies end-to-end. Do not back
up only the live `events.log` if rotation is enabled — the chain would be
incomplete.

If you rotate manually instead of via `KB_AUDIT_MAX_BYTES`, do it while the
process is running so the in-memory last-hash carries forward:

```bash
kubectl -n data-olympus exec data-olympus-mcp-0 -- sh -c \
  'mv /state/audit/events.log /state/audit/events-$(date +%Y%m%dT%H%M%S).log'
# The next append starts a fresh live file linked to the prior tail hash.
```

Prefer `KB_AUDIT_MAX_BYTES` (automatic rotation) so the boundary link is always
written by the server itself.

### 2.3 Backing up pending proposals + unpushed commits

```bash
# Pending queue and push queue (small; safe to snapshot live).
kubectl -n data-olympus exec data-olympus-mcp-0 -- \
    tar -czf - -C /state pending push-queue > state-queues-$(date +%Y%m%d).tar.gz

# Detect unpushed session commits before decommissioning a pod:
kubectl -n data-olympus exec data-olympus-mcp-0 -- \
    sh -c 'cd /kb-main && git fetch -q origin && \
           for wt in /kb-worktrees/*/; do \
             [ -d "$wt/.git" ] || [ -f "$wt/.git" ] || continue; \
             git -C "$wt" log --oneline origin/main..HEAD 2>/dev/null; \
           done'
```

If that last command prints commits, they are **not** on the remote. Let the push
queue drain (watch `push_queue_size` reach 0) before deleting the pod or its PVCs,
or the commits are lost. A restart re-runs startup recovery, which re-enqueues
orphaned commits reachable from a session HEAD but not from `origin/main`.

### 2.4 Full-volume backup (belt and braces)

The PVCs are `ReadWriteOnce`. For a cold full backup, scale the writer to 0 (so no
in-flight writes), snapshot the volumes with your storage layer's snapshot feature
(or `tar` each mount), then scale back to 1:

```bash
kubectl -n data-olympus scale statefulset/data-olympus-mcp --replicas=0
# ... snapshot kb-main / kb-state / kb-worktrees / kb-index PVCs ...
kubectl -n data-olympus scale statefulset/data-olympus-mcp --replicas=1
```

`kb-index` is disposable: the index rebuilds from `/kb-main` on startup, so it need
not be backed up (see §3.3).

---

## 3. Upgrade

### 3.1 Bump the image tag

Edit the image reference in `deploy/k8s/statefulset.yaml` (BOTH the `prepare-git`
initContainer and the main container — keep them on the same tag) and re-apply:

```bash
kubectl apply -k deploy/k8s/
kubectl -n data-olympus rollout status statefulset/data-olympus-mcp
```

Pin to an immutable released tag or a digest for reproducibility. For opt-in
auto-update, use a mutable channel tag with `imagePullPolicy: Always` and a
scheduled `kubectl rollout restart`.

### 3.2 Taxonomy / indexed-prefix compatibility (from the 0.2.0 changelog)

The taxonomy and the set of writable prefixes are configurable at deploy time with
no code change. If you set any of these, they must stay **consistent across an
upgrade** or the index will classify/accept documents differently:

- `KB_TAXONOMY_PATH` — JSON file of the tier/category taxonomy.
- `KB_INDEXED_PREFIXES` — comma-separated writable top-level prefixes that
  **replace** the default set. A prefix removed here makes previously-indexed docs
  under it invisible after the next rebuild.
- `KB_MEMORY_INBOX_PREFIX` — directory new memory proposals are written under.

Before upgrading, diff the new release's default taxonomy against yours; if the
release changes defaults and you rely on them (rather than setting the env vars
explicitly), pin the values in the ConfigMap so the upgrade does not silently move
documents between categories.

### 3.3 Index schema rebuilds

The index (`kb-index` PVC) rebuilds automatically on startup and on every git
refresh that changes the source commit; a schema-version bump in a new release
triggers a full rebuild transparently. You do **not** need to migrate the index
across upgrades.

To force a rebuild (e.g. after manually editing `/kb-main`, or to clear a
suspected corrupt index):

```bash
# Delete the DB and restart; the server rebuilds from /kb-main on boot.
kubectl -n data-olympus exec data-olympus-mcp-0 -- rm -f /index/kb.db
kubectl -n data-olympus rollout restart statefulset/data-olympus-mcp
```

The rebuild uses an atomic swap: if it fails (e.g. a duplicate id), the previous
index is preserved and `last_index_build_status` goes `failed` rather than serving
an empty index.

---

## 4. Recovery playbooks

### 4.1 Degraded / `fetch_failed`

**Symptom:** `/api/v1/health` returns `degraded: true`; `last_git_fetch_status` is
`fetch_failed` or `ff_failed`; `staleness_seconds` climbing. `/readyz` stays 200
(reads still work off the last-good index).

**Cause:** the git remote is unreachable (network, auth, deploy-key), or
`origin/main` diverged from the local `/kb-main` so a fast-forward is impossible.

**Recovery:**

1. Check the deploy key and remote reachability from inside the pod:
   ```bash
   kubectl -n data-olympus exec data-olympus-mcp-0 -- \
     sh -c 'cd /kb-main && GIT_SSH_COMMAND="ssh -i /state/git-key -o UserKnownHostsFile=/state/known_hosts" git fetch -v origin'
   ```
2. `fetch_failed` → fix networking / the deploy key (rotate the Secret and restart
   if the key is revoked). See `SECURITY.md` for the key flow.
3. `ff_failed` → local `/kb-main` diverged from `origin/main`. This usually means a
   history rewrite on the remote (see §4.3) or a local commit that never pushed.
   Resolve per §4.3.

Once the remote is reachable again the pull loop advances `last_git_pull_at`,
staleness resets, and `degraded` clears on the next interval.

### 4.2 Index build failed (`last_index_build_status: failed`)

**Symptom:** `last_index_build_status == failed`; `last_index_conflicts` lists
duplicate ids. The last-good index is still served (atomic swap), so `/readyz`
stays 200, but new content is not indexed.

**Cause:** two docs share a governance `id`, or another build-time validation
failure.

**Recovery:** fix the offending documents on the remote (the conflict list names
the ids and paths), let the pull loop pick up the fix, and the next rebuild
succeeds. Do not delete the index to "retry" — that would drop the last-good index
and leave nothing to serve until the source is fixed.

### 4.3 Force-push / history rewrite on `origin/main`

**Symptom:** after someone force-pushed or rewrote `origin/main`, the pod's
`/kb-main` cannot fast-forward (`ff_failed`); worktrees may hold commits based on
the old history.

**Recovery (destructive; do it deliberately):**

1. Quiesce writes: `kubectl -n data-olympus scale statefulset/data-olympus-mcp --replicas=0`.
   (Confirm the push queue is empty first, or you will lose unpushed commits — see
   §2.3. If you need to preserve them, `git format-patch`/`bundle` them out of the
   worktrees before proceeding.)
2. Scale back up. On boot, hard-reset `/kb-main` to the rewritten remote and clear
   stale worktrees + session branches so sessions re-checkout cleanly:
   ```bash
   kubectl -n data-olympus scale statefulset/data-olympus-mcp --replicas=1
   kubectl -n data-olympus exec data-olympus-mcp-0 -- sh -c '
     cd /kb-main &&
     GIT_SSH_COMMAND="ssh -i /state/git-key -o UserKnownHostsFile=/state/known_hosts" git fetch -q origin &&
     git reset --hard origin/main &&
     git worktree prune'
   # Remove per-session worktrees and their kb-session/* branches so a returning
   # session recreates them against the new history:
   kubectl -n data-olympus exec data-olympus-mcp-0 -- sh -c '
     rm -rf /kb-worktrees/* &&
     cd /kb-main && for b in $(git branch --list "kb-session/*" | tr -d " *"); do git branch -D "$b"; done'
   kubectl -n data-olympus rollout restart statefulset/data-olympus-mcp
   ```
3. Force a fresh index rebuild if needed (§3.3).

The worktree GC would eventually reconcile some of this on its own, but after a
history rewrite an explicit reset is the safe, fast path.

### 4.4 Frozen push-queue entries

**Symptom:** `push_queue_frozen > 0`. An entry hit the retry cap (a persistent
push failure the non-FF rebase recovery could not handle — typically auth,
network, or a remote-side rejection) and the retry loop stopped retrying it.

**Recovery (file-level; there is no unfreeze API):** fix the underlying push
failure, then edit the entry on the push-queue volume (`KB_PUSH_QUEUE_ROOT`,
default `/state/push-queue`):

- **Requeue:** in the entry's `<sha>.json`, set `"frozen": false` and `"attempts": 0`
  (or delete the file and let startup recovery re-enqueue the commit), then the
  retry loop picks it up on its next pass.
- **Drop:** delete the entry's `<sha>.json`. The commit stays on its session
  worktree branch but is never published.

### 4.5 Push-queue demotions to pending (PR #89)

**Symptom:** a `push_conflict_demoted` audit event; a new **pending** entry appears
in `kb_list_pending` that the agent did not create; `pending_count` rose without a
new low-confidence proposal.

**Cause (this is normal, not a fault):** a second overlapping session moved
`origin/main` between a session's commit and its push and the push was rejected
non-fast-forward. Rather than retry forever, the push loop **demoted** the commit
to a pending proposal for operator resolution: it re-proposed the postimage as a
pending edit (carrying the base the commit sat on), removed the push-queue entry,
and recorded a `push_conflict_demoted` audit event (status `demoted_to_pending`).
Two situations reach this same demotion path and emit the same audit
event_type/status:

- **Rebase conflict** — the automatic fetch+rebase could not re-apply the commit
  because both sessions edited the same lines.
- **Persistent contention** — a non-conflicting non-FF that kept losing the race
  is retried in-line for a bounded number of passes, then demoted the same way
  instead of counting toward the freeze cap.

Both surface identically (one `push_conflict_demoted` status; the pending entry's
`reason` is a generic "demoted from push queue after rebase conflict"), so do not
rely on the audit record to tell the two apart. To confirm which occurred, inspect
the push loop logs around the demotion timestamp: a rebase conflict logs the
conflicting paths, whereas contention logs repeated non-FF retry passes.

**Recovery:** resolve the demoted pending entry like any other. Approving re-applies
the change on the current base, subject to the CAS / content-validation gates:

```bash
kb pending                                  # find the demoted entry's id
kb resolve <pending_id> --decision approve  # re-apply on current origin/main
# or --decision reject to abandon it.
```

If approval returns `rejected_stale_base`, the target moved again; re-fetch the
current content, reconcile the change by hand, and re-propose.

### 4.6 Orphaned advisory path locks

**Symptom:** writes to a specific path return `rejected_path_lock_busy` /
`rejected_path_locked` even though no proposal for that path is in flight;
`path_locks_held` in health is non-zero with no matching pending entry.

**Cause:** a crash between acquiring the per-path advisory lock and writing the
pending entry left a lock with no owner.

**Recovery:** the pending GC loop reclaims orphaned locks automatically each pass
(every 5 minutes), logging `pending_gc reclaimed N orphaned path lock(s)`. If you
cannot wait, remove the stale lock file under `KB_PENDING_ROOT/locks` (default
`/state/pending/locks`) for the affected path, then retry the write. Only do this
after confirming no operator is mid-resolve on that path.

---

## 5. Maintenance ledger

A committed, frontmatter-only markdown doc (default path
`tooling/maintenance-ledger.md`, `KB_MAINTENANCE_LEDGER_PATH`) records a
corpus-state audit computed at every index build:

- `status_present_in_all_kb_entries` — whether every indexed document (except
  reserved filenames — `index.md`/`log.md`/`template.md`) carries a `status`
  field, plus a capped list (50 paths + a total count) of the ones that don't.
  This is the migration vehicle for making `status` mandatory.
- `recently_expired` / `expiring_soon` — documents whose `valid_until`
  (issue #107 validity metadata) fell in the last `KB_MAINTENANCE_RECENTLY_EXPIRED_DAYS`
  days (default 30), or falls within the next `KB_MAINTENANCE_EXPIRING_SOON_DAYS`
  days (default 30), each capped at 50 items + a total count.

The ledger is server-side and best-effort: when the computed state CHANGES
since the last committed copy, it is committed through the same serialized
write/commit machinery every other write uses (system agent identity
`data-olympus-system`, a normal `maintenance_ledger` audit event). A commit
failure is logged and audited but never breaks index refresh or serving; it is
retried on the next `git_pull_loop` tick. Recomputation is checked on every
tick, not only when a new remote commit arrives, so a fresh deployment gets
its first ledger commit promptly even before anyone else ever pushes to the
KB. Duplicate commits are guarded three ways, all comparing the structured
state (never the rendered markdown, whose `computed_at` timestamp changes
every render): an in-process last-committed memo, the ledger copy in the live
index, and the system worktree's HEAD copy (which survives restarts), so a
slow push or short sync interval can never re-commit an unchanged state. A
`KB_MAINTENANCE_LEDGER_PATH` outside the configured indexed prefixes is
refused with a `skipped_bad_path` audit event instead of committing a doc the
index would never serve.

The same computed state also drives a `pending_actions` field on `kb_consult`
and `kb_health` responses (never `kb_search` — per-hit noise trains agents to
ignore it): a list of `{kind, message, count}` items, present only while the
corpus is dirty. An agent seeing `pending_actions` should surface it to the
operator and act on it only with operator confirmation. Silencing is
automatic: fix the underlying doc(s), the next index build flips the flag, the
next ledger commit records it, and `pending_actions` disappears — no manual
acking.

A private deployment using a custom `KB_TAXONOMY_PATH` (rather than the
built-in default taxonomy) must make sure `KB_MAINTENANCE_LEDGER_PATH` still
resolves inside an INDEXED prefix, or the ledger is committed but never
searchable/gettable via `kb_search`/`kb_get`.

`kb health` (the CLI) shows any open `pending_actions` in its plain-text
(`-o plain`) summary; `-o json` passes the field through unchanged.

Relevant environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `KB_MAINTENANCE_LEDGER_PATH` | `tooling/maintenance-ledger.md` | Committed ledger doc path (must resolve inside an indexed prefix) |
| `KB_MAINTENANCE_RECENTLY_EXPIRED_DAYS` | `30` | Window (days) for the "recently expired" bucket |
| `KB_MAINTENANCE_EXPIRING_SOON_DAYS` | `30` | Window (days) for the "expiring soon" bucket |

### 5.1 Migrating a corpus to mandatory `status` (issue #114)

`status` has always been a required frontmatter field (SPEC.md section 4.2) and a `kb lint` error when absent. Issue #114 closed the one gap left open: the write path previously let a brand-new status-less document through even though `kb lint` would already flag it. As of this change, `kb_propose_edit` rejects a postimage that creates a **new** file without `status` (`rejected_invalid_document`, reason `missing_status`); editing an **existing** status-less document is still allowed with no `status` required, so a legacy corpus is never locked out of incremental fixes. `kb_propose_memory` is unaffected — every server-rendered memory already stamps `status: proposed` (issue #109).

This is a migration, not a hard break: a legacy corpus with status-less documents keeps working exactly as before.

- **Nothing stops serving.** A status-less document is still indexed and still returned by a default `kb_search` / `kb_get`. It is simply never in force (`IN_FORCE_STATUSES` membership already excludes an absent status), so it can never be surfaced by `kb_consult` and never outranks or governs anything.
- **The migration vehicle is the maintenance ledger** (section 5 above): `status_present_in_all_kb_entries` goes `false` and `missing_status.paths` lists up to 50 offending files (plus a total count) the moment any document lacks `status`. The `pending_actions` CTA on `kb_consult`/`kb_health` nags with a `missing_status` item until the corpus is clean, then disappears automatically — no manual acking.

**Runbook:**

1. Upgrade to a data-olympus version that ships this write-path check.
2. Run `kb lint <bundle-root>` (or `data-olympus lint`) and read the `missing required field 'status'` errors, OR pull the capped list straight from the ledger: `curl -s 'http://<host>/api/v1/health?verbose=true' | jq '.pending_actions'` (see section 6) or read the committed `tooling/maintenance-ledger.md` doc directly.
3. Fix each listed file: add a `status` value from the controlled vocabulary (`draft`, `active`, `deprecated`, `superseded`, `proposed`, `accepted`, `rejected`) via `kb_propose_edit` (edits to an existing status-less file are unaffected by the new write-path check) or directly in the bundle if you write straight to git.
4. Re-run `kb lint`; once every document lints clean, the next index build flips `status_present_in_all_kb_entries` to `true`, the ledger commits the clean state, and `pending_actions` stops appearing.
5. New documents created from this point forward are rejected at write time if `status` is missing, so the corpus cannot regress.

## 6. Governed-lane recap and hook wiring (issue #112)

Governed-lane write protection (see `docs/serving.md` and `SECURITY.md`)
demotes some writes to pending instead of committing them. Three surfaces
make sure a demotion is never missed:

### 6.1 `kb_session_recap` / `kb session-recap` / `GET /api/v1/session-recap`

A read-only per-session write tally over the audit log: how many writes for
`source_session` were `committed`, how many were `demoted_to_pending`
(parked as pending -- whether for a governed-lane demotion or a plain
low-confidence proposal, both mean "awaiting operator review"), and how
many were `rejected` (any `rejected_*` status).

```bash
kb session-recap my-session-id
# {"source_session": "my-session-id", "committed": 3, "demoted_to_pending": 1, "rejected": 0}
kb session-recap my-session-id -o plain
# committed: 3 | demoted_to_pending: 1 | rejected: 0
curl -s "http://<host>/api/v1/session-recap?source_session=my-session-id"
```

Same auth posture as `/api/v1/pending` / `/api/v1/audit`: open by default,
requires a `Bearer` token when `KB_AUTH_TOKEN`/`KB_AUTH_PRINCIPALS` is
configured. The scan walks rotated audit segments too (bounded, so a very
long session history does not turn one query into an unbounded read).

### 6.2 `kb_consult`'s `pending_actions` envelope

`kb_consult` already computes the calling session's recap on every call.
When `demoted_to_pending > 0` for that `source_session`, a `demoted_writes`
item is appended to the response's `pending_actions` list (the same CTA
envelope the maintenance ledger uses -- see section 5) alongside any
maintenance items; omitted entirely when the session has no open
demotions. An agent that calls `kb_consult` in its normal course of work
(as the enforcement gate already requires for a governed decision) sees the
demotion without any extra step.

### 6.3 SessionEnd/Stop hook (`bin/kb-session-recap-hook`)

`bin/kb-session-recap-hook` is a small standalone script that calls the
recap endpoint and prints one line (nothing at all when the session had no
committed/demoted/rejected writes, so a read-only session stays silent):

```
[KB] session recap: 3 committed, 1 awaiting operator review, 0 rejected
```

It resolves `SOURCE_SESSION` from (in order) a positional argument, or the
`session_id` field of a JSON hook payload piped to stdin (the Claude Code
hook contract). It never blocks or fails the agent's shutdown: a missing
session id, an unreachable endpoint, or a malformed response is swallowed
silently (exit 0).

**Wiring it as a Claude Code SessionEnd hook** (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/data-olympus/bin/kb-session-recap-hook"
          }
        ]
      }
    ]
  }
}
```

Claude Code pipes the hook event JSON (including `session_id`) to the
command's stdin, matching this script's default stdin-JSON resolution.

**Wiring it for Codex CLI:** add the equivalent Stop/session-end hook entry
in `~/.codex/config.toml` per Codex's own hook documentation, invoking this
same script. If Codex's hook contract does not pipe JSON to stdin the way
Claude Code's does, pass the session id as the positional argument instead
(`kb-session-recap-hook "$CODEX_SESSION_ID"`, adapted to however Codex
exposes it to the hook command).

Set `KB_ENDPOINT` / `KB_AUTH_TOKEN` in the hook's environment the same way
`bin/kb` reads them.

## 7. Quick reference

| Task | Command |
|---|---|
| Health / alerting | `curl -s http://<host>/api/v1/health` |
| Readiness (probe) | `curl -s http://<host>/readyz` |
| Verify audit chain | `curl -s http://<host>/api/v1/audit/verify` |
| Force index rebuild | `kubectl -n data-olympus exec data-olympus-mcp-0 -- rm -f /index/kb.db && kubectl -n data-olympus rollout restart statefulset/data-olympus-mcp` |
| Upgrade | bump image tag in `statefulset.yaml`, `kubectl apply -k deploy/k8s/` |
| Backup audit | `kubectl -n data-olympus exec data-olympus-mcp-0 -- tar -czf - -C /state audit > audit.tar.gz` |
| Enable audit rotation | set `KB_AUDIT_MAX_BYTES` in the ConfigMap |
| Enable proxy headers | set `KB_TRUSTED_PROXIES` in the ConfigMap |
| Enable Ingress | set `KB_AUTH_TOKEN`, uncomment `- ingress.yaml` in `kustomization.yaml` (see `deploy/k8s/README.md`) |
| Session write recap | `kb session-recap <source_session>` |
| Disable governed-lane protection | set `KB_GOVERNED_LANE_PROTECTION=off` in the ConfigMap |
| View maintenance-ledger state | `curl -s 'http://<host>/api/v1/health?verbose=true' \| jq .pending_actions` |
