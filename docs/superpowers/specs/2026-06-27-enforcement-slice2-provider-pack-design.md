# Design: Enforcement slice 2 (cross-agent provider pack)

Date: 2026-06-27
Status: proposed (awaiting operator review)
Author: brainstormed + verified with Claude Code

## Context

Slice 1 (PR #21) shipped the enforcement core (consult ledger, `kb_consult` /
`kb_gate_check` / `kb_compliance`, compliance audit) and the Claude Code
reference shim plus the `kb enforce install|uninstall|status|doctor` CLI with a
provider interface. Slice 2 adds one provider per remaining locally-installed
agent so `kb enforce install --agent <name>` configures that agent's native
enforcement wiring.

The architecture is unchanged from the master spec
(`2026-06-27-mandatory-consultation-enforcement-design.md`): native per-agent
hooks are the foundation; the optional egress proxy (slice 3) is not in scope.

## Verification-first result (ground truth, installed binaries)

Every hard-provider contract below was verified against the binary installed on
this laptop, not just web docs. Versions: codex 0.142.3, gemini 0.46.0, opencode
1.17.10, copilot 0.0.354, gh-copilot v1.1.1.

| Agent | Tier (verified) | Mechanism |
|---|---|---|
| Claude Code | hard (slice 1) | settings.json hooks |
| Codex CLI | **hard** | `~/.codex/hooks.json` PreToolUse/UserPromptSubmit/SessionStart/Stop |
| Gemini CLI | **hard** | `~/.gemini/settings.json` hooks: BeforeTool + SessionStart |
| OpenCode | **hard** | TS plugin `~/.config/opencode/plugin/*.ts`, `tool.execute.before` |
| Copilot CLI | soft | instructions file + MCP |
| Copilot IDE (VS Code) | soft (repo-scoped) | `.github/copilot-instructions.md` + MCP |
| Antigravity | deferred | no documented hook/instructions/MCP surface |

### Codex CLI 0.142.3 (HARD)

- Hook config: `~/.codex/hooks.json`. Event keys are PascalCase. Full event set:
  PreToolUse, PostToolUse, PermissionRequest, PreCompact, PostCompact,
  SessionStart, UserPromptSubmit, SubagentStart, SubagentStop, Stop. So Codex
  supports the **full loop** (UserPromptSubmit consult + PreToolUse gate), like
  Claude Code.
- Hook entry shape (per event): a list of `{matcher, hooks:[{type:"command",
  command, timeout?, async?}]}`. Matcher is matched against the tool name
  (`Bash|Edit|Write|MultiEdit`).
- stdin JSON for PreToolUse includes: `cwd`, `hook_event_name`,
  `permission_mode`, `session_id`, `tool_input`, `tool_name`, `tool_use_id`,
  `transcript_path`, `turn_id`, `model`.
- Deny contract (two equivalent paths):
  - exit code 2 with the reason on **stderr**; or
  - exit 0 with stdout JSON
    `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}`
    (`permissionDecision` in allow|deny|ask; top-level `decision` in
    approve|block also works). `additionalContext` injects context when allowing.
- **Trust mechanism (operational gotcha):** a newly-added hook command must be
  trusted before Codex runs it. Trust is persisted as a `trusted_hash` per hook
  under `[hooks.state]` in `~/.codex/config.toml`. First run prompts the
  operator to trust it; automation can pass `--dangerously-bypass-hook-trust`.
  The installer MUST surface this (it cannot silently make Codex run the hook).
- **Merge requirement:** `~/.codex/hooks.json` is already populated on this
  laptop (a `protect-files.py` PreToolUse hook). The provider MUST merge, never
  clobber.
- Context injection: `~/.codex/AGENTS.md` is loaded (confirmed). MCP via
  `[mcp_servers.<name>]` in config.toml; data-olympus already registered.

### Gemini CLI 0.46.0 (HARD)

- Config: `~/.gemini/settings.json`, top-level `hooks` map and `mcpServers` map
  (already has 8 servers incl. data-olympus). Project-level
  `<repo>/.gemini/settings.json` also supported.
- Pre-tool event: `BeforeTool` (NOT PreToolUse). Entry shape:
  `{matcher:"<regex on tool name>", hooks:[{type:"command", command, timeout?,
  name?}]}`. Mutating tools: `write_file`, `replace`, `run_shell_command`
  (MCP tools are `mcp_<server>_<tool>`).
- stdin JSON: `session_id`, `transcript_path`, `cwd`, `hook_event_name`,
  `timestamp`, `tool_name`, `tool_input` (note: `tool_input`, not file_path
  directly; the path is inside tool_input for write_file/replace).
- Deny contract: exit 0 with stdout `{"decision":"deny","reason":"..."}`
  (`"block"` is an alias), or exit code 2 with reason on stderr.
- SessionStart: stdin adds `source` (startup|resume|clear); inject context via
  stdout `{"hookSpecificOutput":{"additionalContext":"..."}}`. SessionStart is
  advisory (cannot block startup). Verified live (zero model cost).
- The `gemini hooks` subcommand in 0.46.0 only exposes `migrate`; no `hooks run`
  debug command, so verification is by configuring + running.

### OpenCode 1.17.10 (HARD)

- Plugin contract (installed `@opencode-ai/plugin@1.1.25`):
  `"tool.execute.before"?: (input:{tool,sessionID,callID}, output:{args}) =>
  Promise<void>`. The hook is awaited; **throwing aborts** the tool before it
  runs (verified: no try/catch around the trigger in the compiled binary). Bun
  runtime, so `fetch()` works inside the hook.
- Install: drop a `*.ts`/`*.js` file at `~/.config/opencode/plugin/` (singular;
  plural `plugins/` also accepted) exporting a `Plugin`, or register a path in
  the `plugin` array of `~/.config/opencode/opencode.json`. Uninstall removes the
  file. MCP via the `mcp` block (data-olympus already registered).
- Context injection: there is NO top-level `session.created` hook; use the
  generic `event` hook (`event.event.type === "session.created"`) and
  `experimental.chat.system.transform` to mutate the system prompt.
- **Enforcement gaps to document (not bugs we can fix here):** the subagent
  bypass (#5894) is CLOSED/refuted (subagent tools ARE gated), BUT tools invoked
  via the batch tool bypass `tool.execute.before`, and a file write done via the
  `bash` tool (`cat > f`) arrives as `tool:"bash"`. The plugin should gate
  `bash` too, and we document that batch-path writes are not gated.

### Copilot CLI 0.0.354 / Copilot in VS Code (SOFT)

- No pre-action blocking hook usable from a local install (the Copilot SDK's
  `onPreToolUse` requires building/hosting a custom agent, which replaces the
  user's agent and is out of scope).
- Soft mechanism: an instructions file the agent reads, plus MCP registration of
  data-olympus so `kb_consult`/`kb_gate_check` are available, plus reliance on
  the compliance audit to detect non-consultation.
  - Copilot CLI: `~/.copilot/` config for MCP; an instructions file it reads
    (verify the exact path during implementation; candidate
    `~/.copilot/copilot-instructions.md` or a project `.github/copilot-instructions.md`).
  - Copilot in VS Code: `.github/copilot-instructions.md` (repo-scoped, Markdown)
    + MCP. This provider is therefore **per-repository**, not a global laptop
    install. The installer must treat its target as the current repo.

### Antigravity (DEFERRED)

No public hook, instructions-file, or MCP configuration surface was found
(antigravity.google and developers.google.com/antigravity expose no API docs as
of 2026-06-27). The provider returns a clear "unsupported pending vendor
extensibility docs" message and exits non-zero. Revisit when Google publishes an
extensibility API.

## Design

### Provider interface (already exists; generalize)

`bin/_kb_enforce.py` gains a provider registry keyed by agent name. Each provider
implements:
- `detect()` -> is the agent installed + the config target path
- `render()` -> the managed config/content for this agent
- `install()` / `uninstall()` / `status()` -> idempotent, backed-up, surgical
- `tier()` -> hard | soft
- `verify(endpoint)` -> doctor check

The Claude Code provider from slice 1 is refactored to fit this registry without
behavior change.

### Hook dispatcher generalization (`bin/kb-enforce-hook`)

Codex and Gemini are shell-hook agents whose I/O differs from Claude. Rather than
three near-duplicate scripts, generalize the dispatcher with an **agent dialect**
argument: `kb-enforce-hook <mode> --dialect <claude|codex|gemini>`. The dialect
controls:
- stdin field extraction (Claude/Codex: `tool_input.file_path`; Gemini:
  `tool_input` shape per tool);
- the deny signal:
  - claude/codex: exit 2 + stderr reason (already implemented for claude);
  - gemini: exit 0 + stdout `{"decision":"deny","reason":...}`;
- the context-injection output for the session/prompt modes:
  - claude: stdout text;
  - codex: stdout text or `additionalContext`;
  - gemini: stdout `{"hookSpecificOutput":{"additionalContext":...}}`.

The HTTP calls to `/api/v1/consult` and `/api/v1/gate/check`, the fail-open/closed
logic, the HTTP-status handling, and the KB_AUTH_TOKEN handling are shared
(reused verbatim from the slice-1 hardened dispatcher).

### OpenCode plugin

Ship a versioned TS plugin template `bin/opencode/data-olympus-gate.ts` (the
typechecked file from verification). The OpenCode provider copies it to
`~/.config/opencode/plugin/data-olympus-gate.ts` with a managed header comment
carrying MARKER + SHIM_VERSION; uninstall removes that file only if it carries
the marker. The plugin gates `edit|write|patch|multiedit` and `bash`, calls
`/api/v1/gate/check`, honors fail-open/closed via an env var, and reads
KB_ENDPOINT/KB_AUTH_TOKEN from the environment.

### Soft providers (Copilot CLI, Copilot IDE)

A soft provider writes a managed block into the agent's instructions file
(delimited by the same `# >>> data-olympus enforce (managed) >>>` markers) that
instructs the agent: before any code/architectural decision, call the
`kb_consult` MCP tool for the workspace and follow the returned governing rules;
treat them as authoritative. It also ensures data-olympus is registered as an
MCP server for that agent. `status` reports tier=soft and notes that enforcement
is advisory + audit-based, not blocking.

### Installer UX additions

- `kb enforce install --all` detects every installed agent and installs each at
  its achievable tier, printing a per-agent tier summary and any operator action
  required (notably the Codex trust prompt).
- `kb enforce status` lists every detected agent with tier + install state +
  drift.
- `kb enforce doctor` per agent: for hard shell-hook agents, round-trips the
  endpoint; for Codex, additionally reports trust state; for OpenCode, checks the
  plugin file is present and the MCP server is registered; for soft agents,
  checks the instructions block + MCP registration exist.

## Failure behavior

Unchanged from slice 1: fail-open with a loud warning by default
(`KB_ENFORCE_FAIL_MODE`), configurable to fail-closed, applied per agent in its
hook/plugin.

## Decomposition (build order)

Each provider is independently shippable. Recommended order (lowest risk and
most reuse first):
1. Refactor `bin/_kb_enforce.py` to the provider registry (Claude provider moves
   in, no behavior change). Generalize `kb-enforce-hook` with `--dialect`
   (claude dialect = current behavior).
2. Codex provider (closest to Claude; same dialect family; adds hooks.json merge
   + trust messaging).
3. Gemini provider (gemini dialect: JSON-stdout deny + additionalContext).
4. OpenCode provider (plugin-file install; different mechanism).
5. Soft providers (Copilot CLI + Copilot IDE).
6. Antigravity stub (returns unsupported).

Each step: TDD, its own commit(s), spec-compliance + code-quality review, and a
CHANGELOG `[Unreleased]` line (mandatory per `.rules/changelog-per-release.md`).

## Testing strategy

- Installer/provider unit tests (pytest): per provider, install into a throwaway
  config (temp HOME / temp file), assert the managed block/file is written,
  idempotent, backed up, surgically removed on uninstall, status reports the
  right tier, and operator content survives. Codex: assert merge preserves a
  pre-existing `protect-files.py` hook entry. Gemini: assert merge preserves
  pre-existing mcpServers/hooks.
- Dispatcher dialect tests (bats): extend the existing mock-server harness; for
  each dialect assert the correct deny signal (codex: exit 2 / Gemini: JSON
  stdout decision:deny) and the correct context-injection output shape, plus the
  HTTP-status and fail-open/closed paths already covered for claude.
- OpenCode plugin: a Node/Bun-free unit is hard; at minimum a typecheck in CI if
  feasible, and a documented manual smoke test. Do not block the slice on a live
  OpenCode run.
- Soft providers: assert the instructions managed block + MCP registration are
  written and removed cleanly.

## Out of scope

- The egress proxy (slice 3).
- Antigravity beyond the unsupported stub.
- Building/hosting a custom Copilot SDK agent (that replaces the user's agent).
- Fixing OpenCode's batch-tool/bash-write gaps upstream (documented, not fixed).
- A live end-to-end model run of each agent (verification used binary/docs +
  zero-cost smoke tests; full E2E needs the operator's keys).

## Open operator decisions captured during brainstorming

- Provider scope: all five (Codex, Gemini, OpenCode hard; Copilot CLI + IDE soft)
  plus the Antigravity stub.
- Verification: verify-first per provider (done for the three hard agents; their
  contracts are recorded above).
