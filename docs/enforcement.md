# Enforcement (mandatory consultation gate)

data-olympus can act as an enforced gate for code and architectural decisions,
not only an advisory knowledge base. Enforcement is per-agent: it runs in each
agent's hook surface, driven by a shared policy core in the server.

## Server endpoints

- `POST /api/v1/consult`: record a consultation for `(source_session, workspace)`
  and return the governing rules for an intent. Body:
  - `workspace`
  - `intent`
  - `source_session`
  - `agent_identity`
- `POST /api/v1/gate/check`: verdict (`allow` | `consult_required`) for a pending
  code action. Body:
  - `workspace`
  - `session_id`
  - `tool_name`
  - `action_path`
  - `action_diff` (optional)
- `GET /api/v1/compliance`: aggregated enforcement-event counts.

The same three are exposed as the `kb_consult`, `kb_gate_check`, and
`kb_compliance` MCP tools.

## Configuration

- `KB_CONSULT_TTL_SEC` (default 300): how long a consultation stays fresh.
- `KB_ENFORCE_FAIL_MODE` (default `open`): hook behaviour when the server is
  unreachable. `open` allows the action with a warning; `closed` blocks it.

## Installing the Claude Code gate

```bash
kb enforce install --agent claude-code   # idempotent; backs up settings first
kb enforce status                        # show install state, tier, version
kb enforce doctor                        # verify the wiring reaches the server
kb enforce uninstall --agent claude-code # surgical removal of the managed block
```

The installer writes a managed hook block (SessionStart, UserPromptSubmit,
PreToolUse, Stop) into `~/.claude/settings.json`, tagged so re-runs never
duplicate entries and uninstall never touches operator-authored settings.

## Per-agent providers

Enforcement is installed per agent. Each agent has its own hook or
instructions surface, so the strength of the gate (its "tier") varies. The
tiers below are honest about what each surface can and cannot block.

| Agent | Tier | What it does |
|---|---|---|
| Claude Code | hard | PreToolUse hook blocks governed `Edit`/`Write`/`MultiEdit`/`NotebookEdit` (exit 2 deny). |
| Codex | hard | PreToolUse hook blocks governed `Edit`/`Write`/`MultiEdit` (exit 2 deny). See the trust note below. |
| Gemini | hard | BeforeTool hook blocks governed `write_file`/`replace`/`run_shell_command` (JSON-stdout deny). |
| OpenCode | hard (with caveat) | `tool.execute.before` plugin throws to abort governed `edit`/`write`/`patch`/`multiedit`/`bash`. See caveats below. |
| Copilot CLI | soft | Managed instructions block (advisory) plus MCP; compliance is observed via the audit log, not blocked. |
| Copilot IDE | soft | Managed instructions block in `.github/copilot-instructions.md` (advisory + audit, not blocking). Repo-scoped. |
| Antigravity | deferred (unsupported) | No documented local hook/instructions/MCP surface. See below. |

### Install commands

```bash
kb enforce install --agent codex        # hard, ~/.codex/hooks.json (merges)
kb enforce install --agent gemini       # hard, ~/.gemini/settings.json
kb enforce install --agent opencode     # hard, ~/.config/opencode/plugin/
kb enforce install --agent copilot-cli  # soft, ~/.copilot/copilot-instructions.md
kb enforce install --agent copilot-ide  # soft, .github/copilot-instructions.md (current repo)

kb enforce install --all                # install every supported provider at once
kb enforce status                       # fans out across all agents, one line each
```

`kb enforce status` with no `--agent` reports the install state, tier, and
version for every registered provider. `kb enforce install --all` installs
every supported provider into its default target and skips the unsupported
ones (Antigravity), printing a per-agent tier summary.

### Codex trust note

Installing the Codex PreToolUse hook means Codex will prompt you to TRUST the
hook on its first run. Approve that prompt, or start Codex with
`--dangerously-bypass-hook-trust` for vetted automation. The trust hash
persists under `[hooks.state]` in `~/.codex/config.toml`, so you are only
prompted once per hook version. The installer MERGES the managed block into an
existing `~/.codex/hooks.json`, preserving any operator-authored hooks.

### Gating-coverage caveats

These caveats are deliberate and documented, not bugs:

- Codex gates `Edit`, `Write`, and `MultiEdit`, but NOT the Bash tool. A
  shell-driven write run through Bash bypasses the Codex gate.
- OpenCode gates `edit`, `write`, `patch`, `multiedit`, and `bash`, but
  batch-tool writes are not gated. The plugin gates `bash` specifically to
  narrow this gap. Note that `bash` and `patch` actions carry no file path, so
  path-governed rules cannot classify them: for those two tools the gate is
  advisory (it cannot match a path-scoped governing rule).

### Copilot IDE is repo-scoped (CWD side effect)

`copilot-ide` is SOFT (advisory plus audit, not blocking) and repo-scoped: it
writes `.github/copilot-instructions.md` relative to the CURRENT repo. Because
of this, `kb enforce install --all` creates or edits
`.github/copilot-instructions.md` in the CURRENT working directory, a
CWD-dependent side effect, unlike the home-rooted providers (Claude, Codex,
Gemini, OpenCode, Copilot CLI) whose targets live under `~`. Run `--all` from
the repo whose Copilot IDE instructions you intend to manage.

### Antigravity (deferred)

Antigravity is unsupported. It exposes no documented local hook, instructions,
or MCP surface, so there is nothing for the installer to wire. `kb enforce`
reports it as unsupported and `--all` skips it. Revisit when Google publishes
an extensibility API.
