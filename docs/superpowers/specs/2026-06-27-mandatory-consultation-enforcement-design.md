# Design: Mandatory cross-agent consultation enforcement (slice 1: policy core + Claude Code reference shim)

Date: 2026-06-27
Status: proposed (awaiting operator review)
Author: brainstormed with Claude Code

## Problem

data-olympus is, today, advisory. MCP is pull-based: the server can only respond
when an agent chooses to call `kb_search` / `kb_get`. It cannot force a call,
cannot observe decisions made without calling it, and cannot stop an agent that
ignores it. The operator wants data-olympus to be an authoritative and mandatory
gate for code and architectural decisions across **every** coding agent installed
on the laptop (Claude Code, Codex CLI, Gemini CLI, OpenCode, GitHub Copilot CLI,
Copilot in the IDE, Antigravity, and others), not only Claude Code.

The central constraint: "mandatory" can only be enforced at a layer that sits
between the agent and its actions. That layer is each agent's hook surface, or
below it the outbound LLM network path. MCP alone cannot do it. Enforcement is
therefore necessarily per-agent, with one shared policy brain.

## Decisions locked during brainstorming

- **Architecture: Hybrid (C).** Native per-agent hook shims are the foundation.
  An optional egress proxy is reserved as a last-resort floor for closed IDE apps
  only, and is out of scope for this slice. The default install never decrypts
  network traffic.
- **Enforcement strength: Tiered.** Hard block where the agent supports blocking
  hooks (Claude Code, OpenCode); soft inject plus audit where it does not.
- **Trigger scope: code and architectural decisions** (new library/dependency,
  pattern, migration, API design, schema, auth, refactor), not every prompt.
- **First slice: the policy core in data-olympus plus a Claude Code reference
  shim.** Proves the full gate loop end to end on one agent before fanning out.
- **Failure behavior: fail-open with a loud warning** (configurable to
  fail-closed). A KB outage must not brick the operator's coding; the gap is
  recorded as an audit event.

## The honest ceiling per agent (does not change with effort)

| Agent | Best achievable tier |
|---|---|
| Claude Code | Hard block (PreToolUse) |
| OpenCode | Hard block (PATH wrapper + plugin) |
| Gemini CLI | Medium (inject + partial pre-action check) |
| Codex CLI | Soft (inject + audit) |
| Copilot CLI | Soft (instructions + audit) |
| Copilot IDE / Antigravity | Soft only (context-file injection + MCP availability + audit); no local hard block without the optional proxy, which is itself best-effort against vendor backends |

"Fully mandatory for every agent" is honestly achievable for the hook-capable CLI
agents and best-effort for the closed IDE apps. The design does not pretend
otherwise.

## Overall program (context; only slice 1 is specified in detail here)

1. **Policy core** in data-olympus (this slice): consultation receipt/ledger,
   `policy/check`, intent classifier, compliance audit, the Claude Code reference
   shim, and the `kb enforce` install/uninstall/status/doctor CLI with its agent
   provider interface (Claude Code provider implemented).
2. **Shim pack + installer**: `kb enforce install` generators for every agent.
3. **Optional egress-proxy floor** for closed IDE apps.

## Slice 1 architecture

### The gate loop

```
SessionStart hook       resolve workspace, kb_health warm-up, write onboarding state
UserPromptSubmit hook   classify the prompt; if it is a governed decision:
                          - the HOOK calls kb_consult (consultation done by the
                            harness, not left to the model's goodwill)
                          - inject the matched governing rules into context
                          - server records the consultation in the ledger
PreToolUse hook          on Edit|Write|MultiEdit|NotebookEdit: classify the pending
(code-write tools)        action; if governed:
                          - call kb_gate_check(session, workspace)
                          - fresh consultation on record -> ALLOW
                          - none -> DENY with a message instructing the agent to
                            call kb_consult, then retry
Stop hook                finalize the per-turn compliance audit record
```

Pivotal property: because the UserPromptSubmit hook performs the consultation,
consultation is guaranteed by construction for hook-capable agents. The
PreToolUse gate is defense-in-depth for code edits the prompt classifier missed.

### New server-side components

All reuse the existing index, audit log, and REST/MCP plumbing. No new storage
engine.

- **`kb_consult`** (MCP tool + `POST /api/v1/consult`). Inputs:
  - `workspace`
  - `intent` (the prompt or a summary of it)
  - `source_session`
  - `agent_identity`

  Behavior: run the classifier and retrieval, return the matched governing rules,
  and record a ledger entry keyed by `(source_session, workspace)` with a
  timestamp. Returns:
  - `rules` (the governing standards/decisions)
  - `is_governed_decision`
  - `consulted_at`
  - `ttl_seconds`

- **Consultation ledger.** In-memory map keyed by `(session_id, workspace)` ->
  last consultation timestamp and matched rule ids. TTL-scoped (default: current
  turn / a few minutes, configurable). Single-replica server, so in-memory is
  sufficient. Lost on restart, which only means the next governed edit
  re-consults. Not persisted in slice 1.

- **`kb_gate_check`** (`POST /api/v1/gate/check`). Inputs:
  - `workspace`
  - `session_id`
  - `action_context` (tool name, target path, and diff/summary when available)

  Returns:
  - `verdict` (`allow` | `consult_required`)
  - `rules` (when a fresh consultation exists, echoed for context)
  - `reason`

- **Intent classifier.** Server-side, shared by `kb_consult` and `kb_gate_check`
  so "what counts as a decision" is defined exactly once. Slice 1 implementation
  is a heuristic over:
  - keyword signals (library, dependency, framework, package, pattern,
    migration, refactor, API, endpoint, schema, auth, authorization, RLS, secret)
  - action signals from `action_context` (target file path, extension, diff size)

  Exposed behind a small interface (`classify(intent, action_context) ->
  {is_governed_decision, signals}`) so an LLM-backed classifier can replace the
  heuristic later without touching callers. Optionally surfaced as `kb_classify`
  for debugging.

- **Compliance audit.** Extend the existing append-only audit log with new
  `event_type` values:
  - `consult` (a consultation occurred)
  - `gate_allow`
  - `gate_block` (a governed code action was blocked for missing consultation)
  - `gate_bypass` (a governed decision detected without consultation; emitted by
    soft-tier agents that cannot block)
  - `gate_degraded` (the gate failed open because the server was unreachable;
    emitted by the shim)

  Add a `kb compliance` CLI summary and a `GET /api/v1/compliance` endpoint that
  aggregate these events per agent / session / workspace.

### Claude Code reference shim

- A single small dispatcher script, `kb-enforce-hook`, invoked by all four hooks
  with a mode argument (`session-start`, `user-prompt`, `pre-tool`, `stop`). It
  reads the hook payload on stdin (Claude Code provides `session_id`, cwd, tool
  name and input), resolves the workspace via the existing
  `bin/_kb_detect_workspace.sh`, and calls the endpoints above.
- The shim is generated and wired into Claude Code settings idempotently by
  `kb enforce install --agent claude-code`. This generator is the seed that
  slice 2 generalizes into the cross-agent shim pack.
- Hook wiring:
  - `SessionStart` -> `kb-enforce-hook session-start` (warm-up + state file)
  - `UserPromptSubmit` -> `kb-enforce-hook user-prompt` (classify, consult,
    inject rules via the hook's additional-context output)
  - `PreToolUse` matching `Edit|Write|MultiEdit|NotebookEdit` ->
    `kb-enforce-hook pre-tool` (gate check; deny with guidance on
    `consult_required`)
  - `Stop` -> `kb-enforce-hook stop` (finalize compliance record)

### Install / uninstall via the kb CLI

A gate that is configured by hand is a gate nobody installs correctly. The `kb`
CLI gains an `enforce` command group so configuring (and de-configuring) an
agent's enforcement wiring is a single supported, idempotent, reversible
operation rather than a runbook.

- **`kb enforce install [--agent <name>|--all] [--mode off|soft|hard]`**
  - Detects installed agents (reusing the detection patterns from the existing
    `register-mcp-with-agents.sh` / `_kb_detect_workspace.sh` work) and resolves
    each agent's config location.
  - Installs that agent's shim and hook wiring at the highest tier the agent
    supports, capped by `--mode`. Installing on a soft-only agent installs the
    soft variant and prints that the hard tier is unavailable for it.
  - Idempotent: edits are confined to a clearly delimited managed block
    (`# >>> data-olympus enforce (managed) >>>` ... `# <<< data-olympus enforce
    <<<`) or a dedicated managed file referenced from the agent config, so
    re-running never duplicates entries and never clobbers operator-authored
    settings outside the block.
  - Safety: snapshots the target config to a timestamped backup before any edit;
    refuses to proceed if it cannot write the backup.
  - Stamps a shim/schema version into the managed block so `status` can detect a
    stale install.
- **`kb enforce uninstall [--agent <name>|--all]`**
  - Removes only the managed block / managed file and the dispatcher script,
    leaving the rest of the agent config untouched. Verifies the result parses.
- **`kb enforce status`**
  - Per detected agent, reports: installed yes/no, mode, achievable tier, shim
    version, and whether the on-disk block has drifted from the current version
    (prompting a re-install).
- **`kb enforce doctor`**
  - Live verification: for each installed agent, round-trips `kb_health` and a
    dry-run `kb_gate_check` to confirm the wiring actually reaches the server and
    returns a verdict. This is what turns "I think it's configured" into "it is
    configured and working."

The command surface and an agent **provider interface** (detect -> render config
-> write managed block -> verify) are part of slice 1, with the Claude Code
provider fully implemented. Slice 2 adds one provider per remaining agent; no
new CLI surface is needed for them.

### Configuration

- `KB_ENFORCE_MODE` per workspace: `off | soft | hard` (default chosen at install
  time; allows gradual rollout).
- `KB_CONSULT_TTL_SEC`: freshness window for a consultation (default a few
  minutes).
- Classifier sensitivity knob (keyword set / threshold).
- `KB_ENFORCE_FAIL_MODE`: `open | closed` (default `open`). On `open`, an
  unreachable server allows the edit, prints a visible warning, and emits
  `gate_degraded`.

## Failure behavior

Fail-open with a loud warning is the default. If `kb_gate_check` cannot reach the
server, the PreToolUse hook allows the action, prints a visible warning to the
user, and the shim emits a `gate_degraded` audit event (best-effort; if the
server is down the event is buffered locally and flushed on next reachability).
Configurable to fail-closed via `KB_ENFORCE_FAIL_MODE=closed`.

## Error handling

- Classifier or retrieval errors inside `kb_consult` / `kb_gate_check` return a
  safe default consistent with `KB_ENFORCE_FAIL_MODE` and are logged.
- A malformed or missing hook payload causes the shim to fail-open with a warning
  rather than crash the agent turn.
- The gate never raises an uncaught exception into the agent process; all error
  paths resolve to allow-or-block per the configured fail mode.

## Testing

- Unit: classifier (governed vs non-governed inputs, action-context signals),
  ledger (record, freshness/TTL expiry, key isolation per session+workspace),
  gate verdict logic (fresh consult -> allow, none -> consult_required), audit
  event emission for each new type.
- Integration: `kb_consult` then `kb_gate_check` against a live in-process server
  returns `allow`; `kb_gate_check` with no prior consult returns
  `consult_required`; fail-open path when the server is stubbed unreachable.
- Shim: `kb-enforce-hook` modes against a stubbed endpoint produce the correct
  hook outputs (inject context on user-prompt, deny JSON on pre-tool
  consult_required, allow on fresh consult, warning + allow on degraded).
- End-to-end smoke: install the Claude Code shim into a throwaway settings file,
  drive a governed prompt, confirm the gate blocks an Edit until a consult is on
  record and allows it after.
- CLI: `kb enforce install` into a throwaway config is idempotent (second run is a
  no-op diff) and writes a backup; `kb enforce uninstall` restores the config to
  a clean parseable state with the managed block gone; `kb enforce status`
  reports installed/mode/tier/version and flags a hand-mutated stale block;
  `kb enforce doctor` passes against a live in-process server and reports failure
  against a stubbed-unreachable one.

## Out of scope for this slice

- Shims and `kb enforce` providers for any agent other than Claude Code; the CLI
  command framework and provider interface land here, but the only implemented
  provider is Claude Code (the rest are slice 2).
- The egress proxy (slice 3).
- LLM-backed intent classification (heuristic only here; interface is pluggable).
- Persisting the consultation ledger across server restarts.
- Intent-binding of consultations (a consult authorizes the workspace turn, not a
  specific intent) - deferred refinement.

## YAGNI trims taken

- No signed receipt tokens plumbed through the agent; the server-side ledger keyed
  by session is the single source of truth, which is simpler and avoids a signing
  secret.
- No per-intent scoping in v1; freshness within TTL for the workspace is enough to
  prove the loop.
- No local verdict cache (the fail-closed-with-cache option was declined in favor
  of fail-open).
