# Enforcement (mandatory consultation gate)

data-olympus can act as an enforced gate for code and architectural decisions,
not only an advisory knowledge base. Enforcement is per-agent: it runs in each
agent's hook surface, driven by a shared policy core in the server.

## Gate policy: only an explicit consult clears the gate

The gate means "the agent explicitly consulted the governing rules for this
work", not merely "an HTTP call to /consult happened recently in this session".
Two kinds of consult are recorded:

- **Explicit consult** (`trigger: "explicit"`, the default): a deliberate
  `kb_consult` MCP call (or a `POST /api/v1/consult` with no `trigger`, since an
  old client sending a bare consult is always a real agent action). Only an
  explicit consult that is still fresh (within `KB_CONSULT_TTL_SEC`) satisfies
  the gate for its `(session_id, workspace)` pair.
- **Prompt-hook consult** (`trigger: "prompt_hook"`): the auto-consult the
  per-agent installers fire on every user turn (Claude/Codex `UserPromptSubmit`,
  Gemini `BeforeAgent`) to inject governing rules into the turn's context. It is
  recorded for audit/compliance and its rules are still injected, but it **never
  clears the gate**. Otherwise every user prompt would refresh the ledger and the
  gate would fire only during autonomous stretches longer than the TTL.

A prompt-hook consult never downgrades a still-fresh explicit consult on the same
`(session, workspace)`: the ledger tracks the last explicit consult separately, so
interleaved prompt-hook consults cannot un-clear a gate an explicit consult
cleared.

There is no deadlock for legitimately non-governed intents: `kb_gate_check` only
requires a consult for actions the classifier deems governed, and any explicit
consult (governed or not) records a fresh explicit timestamp, so an agent can
always clear the gate by calling `kb_consult` and then retrying.

## Server endpoints

- `POST /api/v1/consult`: record a consultation for `(source_session, workspace)`
  and return the governing rules for an intent. Body:
  - `workspace`
  - `intent`
  - `source_session`
  - `agent_identity`
  - `trigger` (optional; `"explicit"` default, or `"prompt_hook"` for an
    installer auto-consult — see the gate policy above). Omitting it is treated as
    `"explicit"` for backward compatibility.
- `POST /api/v1/gate/check`: verdict (`allow` | `consult_required`) for a pending
  code action. Body:
  - `workspace`
  - `session_id`
  - `tool_name`
  - `action_path`
  - `action_diff` (optional)

  The response echoes `session_id` and `workspace` (the exact gate key) alongside
  the verdict and reason, so a blocked MCP caller can build the clearing
  `kb_consult` call without guessing the session id. When blocked, `reason`
  contains a copy-pasteable `kb_consult(...)` instruction.
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

`kb enforce doctor` verifies more than endpoint reachability. It also checks that
the managed marker at the current shim version is present in the live settings
file, that the hook dispatcher (`bin/kb-enforce-hook`, or the OpenCode plugin
file) exists and is executable, and it WARNS and fails when the dispatcher path
resolves inside a `.worktrees/` or `.claude/worktrees/` checkout: an install
performed from a worktree dangles after the worktree is pruned and then silently
fails open. If doctor warns about a worktree install, re-run
`kb enforce install` from the main checkout.

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

- Codex gates `Edit`, `Write`, `MultiEdit`, AND `Bash`. Bash is gated so a
  shell-driven write or a dependency-install command run through Bash does not
  bypass the gate. (Bash carries no file path, so only diff/command signals such
  as install commands classify it; see the path note below.)
- Claude Code gates `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, and `Bash`.
  The tool matchers are anchored (`^(...)$`) so they match exactly those tool
  names and do not substring-match unrelated tools such as `BashOutput`.
- OpenCode gates `edit`, `write`, `patch`, `multiedit`, and `bash`, but
  batch-tool writes are not gated. The plugin gates `bash` specifically to
  narrow this gap. Note that `bash` and `patch` actions carry no file path, so
  path-governed rules cannot classify them: for those two tools the gate keys off
  the command/patch content (passed as `action_diff`), not a file path.
  The OpenCode plugin resolves the workspace key to the main git worktree
  basename (the same worktree-invariant key every other surface uses), so a
  consult recorded from the main checkout clears the OpenCode gate too; it no
  longer sends the raw absolute directory path (which could never match a
  consult keyed by basename).

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

## Detection floor (un-hookable agents)

### Why it exists

Some agents cannot be hard-gated locally. Closed IDE apps such as
Copilot-in-VS-Code and Antigravity expose no local hook, instructions, or MCP
surface that the installer can wire, so a PreToolUse-style deny is impossible
for them. What every agent does share is git: every governed change eventually
becomes a commit. Git is therefore the common chokepoint. The detection floor
uses it not to block (the gating tiers above already do that where they can)
but to detect governed changes that have no consultation on record and report
them, so an un-hookable agent's work is at least observable after the fact.

### `kb enforce report` (alias `data-olympus report`)

```bash
kb enforce report [--workspace W] [--range A..B | --since S] \
                  [--window-sec N] [--json] [--fail-on-unverified] [--staged]
```

`data-olympus report` is the same command (the `kb enforce report` route
delegates straight to it). The report parses governed commits from `git log`
(reusing the same path classifier the gates use), then correlates them against
`consult` events fetched from the existing `GET /api/v1/audit`. It reuses that
endpoint as-is: there is no server change for this feature.

Flags:

- `--workspace W`: workspace label to correlate against (defaults to the main
  git worktree basename, identical from the main checkout and any linked
  worktree; falls back to the current directory name outside a git repository).
- `--range A..B`: a git revision range to scan (for example `HEAD~5..HEAD`).
- `--since S`: a git `--since` window when no `--range` is given.
- `--window-sec N`: the correlation window, in seconds, around each commit.
- `--json`: emit machine-readable JSON instead of the text summary.
- `--fail-on-unverified`: exit non-zero when an unverified governed change is
  found (see exit codes).
- `--staged`: classify the staged diff instead of `git log` (used by the
  pre-commit block hook).

Exit codes:

- `0`: normal completion (including the case where unverified changes exist but
  `--fail-on-unverified` was not passed).
- `3`: returned only with `--fail-on-unverified`, when at least one unverified
  governed change is found.
- `2`: a git error (for example a bad `--range`). The command does not mistake
  a git failure for a clean repo.

### The opt-in git hook

```bash
kb enforce install --agent git           # post-commit WARN hook (detection only)
kb enforce install --agent git --block   # pre-commit hook that fails the commit
kb enforce uninstall --agent git         # surgical removal of the managed block
```

`kb enforce install --agent git` installs a post-commit hook that WARNs: it
cannot block (post-commit runs after the commit is already made), so it is pure
detection. `kb enforce install --agent git --block` instead installs a
pre-commit hook that fails the commit when the staged diff contains an
unverified governed change.

The git hooks are repo-scoped: they live in `.git/hooks` of the current repo,
not under `~`. The installer merges its managed block into any existing hook
content (preserving operator-authored lines) and uninstall removes only the
managed block. The git provider is opt-in: it is NOT installed by
`kb enforce install --all`. You must ask for it explicitly with `--agent git`.

### Requirement: `data-olympus` must be on PATH

Both `kb enforce report` and the git hooks call the `data-olympus` console
script. It must be on PATH at run time (for `report`) and at commit time (for
the hooks). Install the package, or activate the venv, so the script resolves.
A GUI git client whose environment lacks the venv PATH will see the warn hook
print `command not found` (harmless: the commit still lands), or, for the
`--block` pre-commit hook, fail the commit because the gate cannot run.

### Honest limits

Correlation is best-effort by workspace plus time window, not a hard
session-to-commit link. State the limits plainly:

- False positives (a governed change reported as unverified when a consult did
  happen): a consult recorded in a different session, a consult that fell
  outside the time window, or a consult recorded under a different workspace
  label.
- False negatives (a governed change that goes unreported): a change whose path
  the classifier does not consider governed.

When the audit endpoint is unreachable, the command degrades to warn: it lists
the governed changes it found and marks the consult state as unknown rather
than crashing. A post-commit warn hook never crashes a commit. The
`--staged`/`--block` gate requires a consult within the window to pass, so a
stale consult (outside the window) does not let a governed commit through.

## Hardening and observability (slice 4)

Slice 4 closes the enforcement follow-ups: it makes the compliance audit
capture gate bypass and degradation, feeds the gate a richer classification
signal, completes the installer CLI, persists the consultation ledger, and adds
a changelog CI gate.

### `gate_bypass` and `gate_degraded` are now recorded

Two new enforcement events make non-compliant or degraded paths observable:

- `gate_bypass`: recorded once per unverified governed change. `data-olympus
  report --emit-events` (and the post-commit git warn hook, which now passes
  `--emit-events`) post one `gate_bypass` per unverified governed change found
  in the scanned range.
- `gate_degraded`: recorded by the pre-tool hook when the gate is REACHABLE but
  degraded (a non-2xx response or an unparseable body). The hook does NOT record
  `gate_degraded` on a full connection failure: it cannot phone home when the
  server is down, so a hard outage leaves no degraded event (the action still
  fails open with a warning, per `KB_ENFORCE_FAIL_MODE`).

`kb_compliance` (and `GET /api/v1/compliance`) now surface both event types in
its aggregated counts. A new auth-guarded `POST /api/v1/audit/event` endpoint
(and the matching `kb_record_event` MCP tool) lets clients append these events.
The endpoint accepts ONLY `gate_bypass` and `gate_degraded`, so a client cannot
forge `consult`, `gate_allow`, or `gate_block` rows. Body:

- `event_type` (must be `gate_bypass` or `gate_degraded`)
- `workspace`
- `agent_identity`
- `source_session`
- `reason`

### Richer gate signal: `action_diff` + word-boundary classifier

The pre-tool hook now sends `action_diff` to the gate: the change content (a
Write's content, an Edit's new string, or a Bash command), capped at 4000
characters so the gate body stays bounded. With this content the classifier can
do two new things:

- Word-boundary keyword matching: keywords are matched on word boundaries, so
  "authored" no longer matches the "auth" keyword and "standardize" no longer
  matches "standard". This removes a class of substring false positives.
- Dependency-install command signals: install commands (`pip install`, `uv
  add`, `npm install`, `apt install`, `brew install`, `go get`, `cargo add`,
  and similar) in `action_diff` are recognized as governed. This lets the
  classifier handle Bash/shell governed actions, which carry their intent in the
  command rather than a file path.

To exercise this, Codex and Claude now also gate the `Bash` tool (added to the
PreToolUse matcher alongside the edit tools).

### `kb enforce install --mode off|soft|hard`

The installer now takes a `--mode` flag:

- `hard` (default): the full gate, including the blocking pre-tool gate.
- `soft`: installs the consult and inject hooks only (SessionStart,
  UserPromptSubmit), with NO blocking pre-tool gate.
- `off`: uninstalls the managed hooks.

`soft`/`hard` apply to the hook-file providers Claude, Codex, and Gemini. The
fixed-tier providers (OpenCode, Copilot CLI, Copilot IDE) accept `off` (to
uninstall) and note that `soft`/`hard` have no effect on their fixed tier:
`kb enforce install --agent opencode --mode soft` prints that note and installs
the hard gate.

### Persisted consultation ledger

The consultation ledger now persists to `KB_LEDGER_PATH` (default
`/state/ledger.json`), so recorded consultations survive a server restart. It
loads the file on startup and rewrites it atomically on every record. With no
path configured it stays purely in-memory (the original behavior), and a
corrupt or unreadable file degrades to empty (with a logged warning) rather than
crashing.

### Friendly PATH hint for `kb enforce report`

`kb enforce report` now prints a friendly hint and exits 127 when
`data-olympus` is not on PATH, instead of leaking a raw `command not found`. The
message points the operator at installing the package or activating its venv.

### Changelog CI gate

CI now guards that a pull request changing functional paths (`src/`, `bin/`,
`deploy/`, or `SPEC.md`) also updates `CHANGELOG.md`. The guard is skippable by
adding a `no-changelog` label to the PR. Docs-only and tests-only changes do not
trip the guard.
