# data-olympus roadmap

Guided onboarding (import, interview, bootstrap, cleanup) shipped as the first
slice of a broader effort to keep team knowledge current, homogeneous, and
retrievable by coding agents. The pieces below are tracked as GitHub issues; this
file is the index.

| Piece | Summary | Status | Tracking |
|-------|---------|--------|----------|
| A | Guided onboarding + discovery capture + dedup/cleanup | Shipped | this repo |
| B | Curation / pattern promotion (`kb_curate`): surface repeated patterns and propose hoisting component to project to universal, human-gated | Planned | #31 |
| C | Consultation telemetry + per-user stats + reporting, privacy-gated (config opt-in, stored off the public KB) | Planned | #32 |
| D | Cross-agent skill distribution: import team-shareable skills from the MCP on first agent run | Planned | #33 |
| E | Skill suggestion: detect repeated instructions and propose a reusable skill or workflow | Planned | #34 |
| F | Remote knowledge bases: consult other instances read only, enabled or blocked per project | RFC, open questions under vote | #287 |
| G | Screen retrieved candidates with a typed-decision model before they reach the agent's context | Research | #288 |

Suggested build order after A: C, then B, then D and E.

F does not depend on the others and is gated on the questions still open on its
issue rather than on any piece here. G is research rather than a commitment: it
needs representative labelled data before it can be evaluated, and C is one
possible source of that.
