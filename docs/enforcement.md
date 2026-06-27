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

## Per-agent ceiling

Hard block is available for Claude Code (and, in a later slice, OpenCode). Other
agents degrade to soft inject + audit. See the design spec for the full table.
