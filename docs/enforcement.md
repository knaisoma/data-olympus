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

`kb_gate_check` only requires a consult for actions the classifier deems
governed. Which explicit consults clear a governed action depends on
`KB_GATE_CLEARANCE`, described in the next section.

## Clearance is bound to the consulted intent (issue #296)

Under `KB_GATE_CLEARANCE=intent`, the default, a fresh explicit consult clears
only the governed actions its intent covers. A consult about the database schema
no longer clears a `pip install` that follows it inside the TTL.

Each explicit consult records a coverage set computed from its `intent` text:

- the keyword signals (`keyword:<kw>`) the classifier finds in the intent;
- a `command:<fragment>` signal for each governed command fragment the intent
  contains, by the same substring rule the gate applies to a Bash command;
- a `path:<glob>` signal for each whitespace or quote delimited token of the
  intent, computed by the same path matcher the gate applies to `action_path`.
  A quoted span counts as one token, so a path containing spaces can be named.

Coverage accumulates per `(session_id, workspace)`: several fresh consults on
different topics all count, and each signal expires on its own TTL. A
prompt-hook consult never adds coverage. A governed action is allowed when
EVERY one of its signals is covered by a fresh coverage signal, either directly
or through the family mapping below. An action that is both a dependency
manifest edit and an install command needs both covered; one dependency consult
does that through the mapping.

Path signals are compared by family, the glob with one leading `*/` removed, so
`path:*/go.mod` (a nested `services/api/go.mod`) and `path:go.mod` are the same
family, `go.mod`.

The family mapping lets a topical consult cover the actions it plainly governs,
without the agent reciting file paths:

| Action signal | Covered by any of |
| --- | --- |
| `path:` dependency manifest families (`pyproject.toml`, `package.json`, `requirements*.txt`, `go.mod`, `Cargo.toml`, `pom.xml`) | `keyword:dependency`, `dependencies`, `package`, `library` |
| `command:` built-in install fragments | the same four keywords |
| `path:` families `migrations/*` and `migration/*` | `keyword:migration`, `migrate`, `schema` |
| `path:` families `schema/*`, `schema.sql` and `*.sql` | `keyword:schema`, `migration`, `migrate` |
| `path:` families `Dockerfile` and `docker-compose*.yml` | the same family only |
| `keyword:<kw>` from a Bash action | the same keyword only |

Operator additions (`KB_GOVERNED_EXTRA_*`) have no mapping and are covered by
the same signal only.

**No deadlock.** Every governed action stays clearable: an intent that contains
the action's path exactly as the gate saw it, or the command fragment, or the
keyword, reproduces the action's signal through the same function. A basename
also works for a manifest family that has a bare twin (`Cargo.toml` clears a
nested `crates/x/Cargo.toml`), but not for `pom.xml`, which is governed only
when nested, or for a directory family such as `migrations/*`; name the full
path or use a mapped keyword for those.

**Denial.** The verdict stays `consult_required`, so hooks and installers are
unaffected. The `reason` lists every uncovered signal and ends with one
copy-pasteable `kb_consult(...)` call whose `intent` covers all of them
together, leading with the exact path when a path signal is uncovered and
quoting the command fragment when a command signal is. The path or fragment is
wrapped in a quote character it does not contain, and each argument of the call
is written as a Python string literal, so the call still parses when a path
holds a quote character. The `gate_block` audit
event keeps `status: consult_required` and adds an `uncovered` field holding the
signal list, so `kb_compliance` counts are unchanged and a mismatch is
countable.

**Audit.** Every `consult` audit event records the consult's coverage set in a
`coverage` field (an empty list when it covers nothing) next to its `trigger`;
`reason` still holds the classifier signals of the intent. `kb enforce report`
reads this field to judge commits by the same rule as the gate (see "Detection
floor" below).

**Not a security boundary.** An agent can name every keyword in one intent and
cover everything. The gate makes consulting the governing rules the path of
least resistance; it does not authenticate intent. A consult still retrieves
the rules for the intent it states, so an over-broad intent returns a broad
rule set.

**Migration.** `KB_GATE_CLEARANCE=pair` restores the earlier rule exactly: any
fresh explicit consult for the `(session_id, workspace)` pair, governed or not,
clears every governed action. A ledger written before this change has no
recorded coverage, so under `intent` a session that consulted before the
upgrade needs one new consult before its next governed action.

## Retrieval is hard-filtered to the in-force class (issue #109)

The rules `kb_consult` returns for a governed intent are retrieved with
`in_force=true` (see `docs/serving.md`'s `in_force` section for the full
predicate), unconditionally: this is not a caller-facing parameter. So an
unreviewed agent-written memory (`status: proposed`, stamped automatically by
`kb_propose_memory`), a superseded/deprecated/rejected decision, an expired or
upcoming document, or a document under the memory-inbox prefix can never be
handed back as "the" governing rule for a decision, no matter what its
frontmatter claims. A document with no `status` field (or one outside the
in-force class) is likewise never returned here, even though it may still
surface via plain `kb_search`/`kb_get`.

## Server endpoints

- `POST /api/v1/consult`: record a consultation for `(source_session, workspace)`
  and return the governing rules for an intent. Body:
  - `workspace`
  - `intent`
  - `source_session`
  - `agent_identity`
  - `trigger` (optional; `"explicit"` default, or `"prompt_hook"` for an
    installer auto-consult; see the gate policy above). Omitting it is treated as
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
  contains a copy-pasteable `kb_consult(...)` instruction and, under intent
  clearance, the uncovered signals and an intent that covers them.
- `GET /api/v1/compliance`: aggregated enforcement-event counts.

The same three are exposed as the `kb_consult`, `kb_gate_check`, and
`kb_compliance` MCP tools.

## Configuration

- `KB_CONSULT_TTL_SEC` (default 300): how long a consultation stays fresh.
- `KB_GATE_CLEARANCE` (default `intent`): what a fresh explicit consult clears.
  `intent` clears only the governed actions the consult's intent covers (see
  above); `pair` clears every governed action for the `(session_id, workspace)`
  pair, the behaviour before issue #296. Case-insensitive; blank means unset;
  any other value fails startup naming the setting.
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
| Claude Code | hard | PreToolUse hook blocks governed `Edit`/`Write`/`MultiEdit`/`NotebookEdit`/`Bash` (exit 2 deny). |
| Codex | hard | PreToolUse hook blocks governed `Edit`/`Write`/`MultiEdit`/`Bash` (exit 2 deny). See the trust note below. |
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
`consult` events fetched from `GET /api/v1/audit`.

A governed commit is judged by the gate's clearance rule, read from
`KB_GATE_CLEARANCE` in the environment where the report runs:

- Under `intent` (the default), the commit is verified only when the explicit
  consults inside its window together cover every signal of its governed
  paths, by the same coverage and family rules as the live gate. Consults in
  the window combine as fresh consults do at the gate, and a prompt-hook
  consult covers nothing. An unverified commit lists its uncovered signals
  (`uncovered` in `--json`).
- A `consult` audit row written before coverage was recorded has no `coverage`
  field, so what it covered is unknown. When such a row is in the window and
  the recorded coverage does not settle the commit, the commit is verified by
  timing alone, as before, and the report says so: a `TIMING ONLY` line in the
  text output and the commit's sha in `timing_only` in `--json`.
- Under `pair`, any consult in the window verifies the commit, the behaviour
  before coverage was recorded.

The `--json` output also carries `clearance`, the rule it applied.

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
- `2`: a git error (for example a bad `--range`), or a `KB_GATE_CLEARANCE`
  value other than `intent` or `pair`. The command does not mistake a git
  failure for a clean repo.

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
  the classifier does not consider governed, or a commit judged by timing only
  against a consult row written before coverage was recorded.
- The window is not a session. A consult by another agent in the same
  workspace and window counts toward a commit's coverage, where the live gate
  counts only the acting session's consults.
- `KB_GATE_CLEARANCE` is read where the report runs, not from the server. Set
  it to the server's value, or the report applies a different rule than the
  gate.

When the audit endpoint is unreachable, the command degrades to warn: it lists
the governed changes it found and marks the consult state as unknown rather
than crashing. A post-commit warn hook never crashes a commit. The
`--staged`/`--block` gate requires a consult within the window that covers the
staged governed paths (any consult in the window under `pair`), so neither a
stale consult (outside the window) nor one about another topic lets a governed
commit through.

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
