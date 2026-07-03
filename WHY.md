# Why data-olympus

This is the longer version of the story we could not fit into a launch post: why
we built data-olympus, what it actually does differently, how it relates to
Google's Open Knowledge Format, and where our own measurements say it is
strongest and where it is not. It is written to be read start to finish, but the
section headings are there if you want to jump.

## The frustration that started it

Anyone who has spent real time building with coding agents has probably felt some
version of this. You write down how things should be done. You are careful about
it. And then the agent half-remembers it. It combines two instructions that were
never meant to go together. It reaches for a rule that does not apply, or picks a
reasonable-looking pattern that your team actually decided against months ago.
Sometimes it simply drops the one constraint that mattered most. And even when it
does surface the right guidance, it tends to treat it as a polite suggestion
rather than a decision the team has already made.

For a while we treated each of these as a prompting problem. We wrote longer
system prompts, pasted in more context, and kept a growing file of rules near the
top of every session. It helped a little, and then it stopped helping. The file
got long, the important lines got buried, and the model still had no way to tell
a current decision from one we had retired a year ago. They were all just text.

At some point it became clear the problem was not the wording of any single
prompt. It was structural. The agent had no durable memory of the team's
decisions. It only had whatever fragments happened to fit in the context window
that day, ranked by nothing in particular, with no notion of which rule was still
in force. So we stopped trying to win the prompt and built a small system to hold
that memory properly. We used its first version internally for several months,
it earned its place, and we eventually pulled it out of our own setup,
generalized it, and turned it into the standalone component we are sharing now.

## What it is, and what it is deliberately not

data-olympus governs decisions, not code. When an agent is about to choose a
library, settle on a pattern, or take a migration path, it surfaces the standard
or the earlier decision that should govern that choice, and it does so with enough
structure that the agent can treat the answer as authoritative rather than
optional.

A small example of what that looks like. An agent is wiring up a new service and
reaches for the first HTTP client it knows. Before it writes the import, it checks
data-olympus, which returns the standard your team accepted last quarter naming a
different client, along with the older decision it superseded that explains why.
The agent uses the right one, and nobody had to remember to put it in the prompt.

It is not a code-search or reference-finding tool, and it does not try to be.
Tools like LSP, grep, and Sourcegraph already answer "where is this used" and
"show me this symbol" very well, and we are happy to leave that to them. The
retrieval task we care about is a different one: going from coding intent to the
governing rule. That is the part of working with agents that current tools
support the least, and it is where a wrong or missing answer costs the most,
because the agent will confidently do the wrong thing and make it look reasonable.

You capture, query, and grow rules at three levels of scope:

- **Universal**: the widest scope you work at, a company, a team, or a guild.
- **Project**: a product, an area, or a section of work.
- **Component**: a single microservice, a library, or a part of one.

The whole knowledge base is a directory of markdown files with YAML frontmatter,
kept in git. There is no database, no proprietary schema, and nothing to lock you
in. A human can read and author a document with no SDK, and so can an agent. Every
change is an ordinary commit, so review is review and history is `git log`.

## What you actually get

data-olympus is three pieces that work together.

- **The format.** A bundle of markdown files with YAML frontmatter, kept in git.
  This is the source of truth, readable and authorable with no special tooling.
- **An MCP server.** It serves the bundle to your coding agents. You can run it
  locally just for yourself, or deploy it once and point your whole team's agents
  at the same endpoint. Every write goes through a single-writer pipeline that
  gives each agent session its own git worktree, so several people's agents can
  read and propose changes at the same time without colliding.
- **A CLI.** A small `kb` client over the same API for the shell and for scripts,
  plus the `data-olympus` commands to lint, index, and visualize a bundle.

Wiring it into an agent is mostly a matter of registering the MCP endpoint, a few
lines in a coding agent's config, so the knowledge base shows up as something the
agent can query and propose to while it works.

## The differentiators, and why they matter

A few properties are the reason we kept using it rather than going back to a long
prompt file.

**It knows the lifecycle of a decision.** Every entry carries an explicit
`status`, and decisions that replace older ones are linked through a `supersedes`
chain. That sounds like bookkeeping until you watch a plain keyword search hand an
agent a rule you retired six months ago because the old document happens to match
the words in the query. Because data-olympus understands which rule is current, a
default search downranks the superseded document below the one that replaced it
(it is still returned, useful when tracing history), and a caller that asks for
only in-force guidance can exclude it from the result set entirely.

**It is queryable in the ways governance actually needs.** You can filter by
`status`, by `tier`, or by `type` without any post-processing, and ask things like
"show me the accepted universal standards" or "what superseded this decision" and
get a direct answer rather than a pile of files to read.

**Several agents can write to it safely.** Contributions go through a single-writer
pipeline with advisory locks, per-session worktrees, and a durable push queue.
Agents capture findings as they work and propose them back, low-confidence
proposals wait for a human to approve, edit, or reject, and concurrent writers do
not clobber each other. The knowledge base grows from real use without turning
into a free-for-all.

**Nothing arrives by surprise.** We deliberately did not build a background
ingestor that pulls in outside sources on its own. The knowledge base holds only
what someone on your team decided to add and reviewed. The tradeoff is that we do
not get automatic breadth, and we think that is the right trade for governing
rules: when an agent treats an entry as authoritative, you want to know a human
put it there on purpose. The payoff is content with no background-ingestion
drift, provenance you can read straight from the git history, and retrieval you
can reproduce and audit.

## How this relates to OKF

By the time Google published the Open Knowledge Format, we had been running our
own format internally for months. Reading the OKF spec was a slightly strange
experience, because it was close to what we had independently landed on: the same
instinct to keep engineering decisions as plain markdown in git and serve them to
agents. That convergence is what made the decision to open up easy. Rather than
keep maintaining a private format that happened to resemble OKF, or build
something positioned against it, we made data-olympus a profile designed to be
readable by OKF consumers and put our governance work on top of the standard
instead of beside it. If OKF becomes a common way to structure knowledge bases,
and we think it is a sensible one, we would rather build on it than around it.

In practice that design intent means a data-olympus bundle inherits OKF's
directory structure, frontmatter conventions, reserved filenames, and link
model, so an OKF consumer that tolerates unknown keys (as the OKF spec
requires) should be able to read it. We have not yet run a real OKF reference
consumer against a data-olympus bundle to confirm that end to end, or the
reverse (importing an OKF-tooling-produced bundle and confirming it governs
cleanly); that executable check is tracked in
[issue #82](https://github.com/knaisoma/data-olympus/issues/82), not shipped
today.

The natural question is then why use data-olympus rather than OKF directly. OKF
defines a deliberately minimal required set (an `id`, a `type`, a `spec_version`)
and no governance fields. What we add sits on top of that baseline:

- A stable `id` that is decoupled from the file path, so reorganizing the tree
  does not change a concept's identity.
- A controlled `type` vocabulary, plus explicit `status` and `tier` fields, each
  with a fixed set of allowed values.
- A `supersedes` and `superseded_by` chain for decisions, so you can trace how
  guidance changed over time.
- A serving and write model: an MCP server and the single-writer pipeline
  described above. OKF specifies neither, by design.

There is one thing OKF does that we deliberately do not. Its reference
implementation ships an automatic producer that can pull structured data from a
warehouse and enrich it from the web to populate a bundle with very little human
authoring. That is a genuine strength if your goal is broad coverage quickly. Our
goal is the opposite end of the same spectrum: curated, reviewed knowledge where
accuracy and governance matter more than breadth. The two approaches compose
well: a bundle the OKF producer fills can be imported and normalized into our
profile, and then governed.

## What the benchmark says

Before sharing this we built a reproducible retrieval benchmark, mostly to check
our own assumptions rather than to win an argument. The honest caveats first: the
corpus is synthetic (250 concepts, deterministically generated), the committed run
uses a dependency-free tokenizer so token ratios across methods are meaningful but
absolute counts are specific to that tokenizer, and dense vector retrieval is
opt-in rather than the default. The full methodology and the numbers are in
[`docs/comparison.md`](docs/comparison.md) and
[`benchmarks/README.md`](benchmarks/README.md), and you can regenerate them. The
table below is generated from the committed results and CI-checked for drift, so
it cannot quietly go stale.

With that said, here is where we are strongest, measured against a plain BM25
keyword baseline and a status-aware BM25 baseline (BM25 that also reads the
governance `status` field) over the same 500 queries:

<!-- BENCH:headline START -->
| What we measured | data-olympus | BM25 | Status-aware BM25 |
|---|---|---|---|
| Tokens sent to the model per query (as-shipped) | 309 | 430 | 424 |
| Tokens under normalized payload policy | 90 | 85 | 85 |
| Overall recall@k | 0.582 | 0.572 | 0.572 |
| Serves-stale rate (retired rule reached the agent) | 0.000 | 0.750 | 0.000 |
<!-- BENCH:headline END -->

Three things stand out, and we are careful about attributing each one honestly.
First, data-olympus answers at competitive (here slightly higher) recall while
sending fewer tokens per query as shipped, and that gap widens as the knowledge
base grows because its payload is an outline plus a few snippets plus one full
document, independent of corpus size. But when we normalize every method to the
same payload policy (charge each the cost of one full document), the token gap
mostly closes, so we say plainly: the as-shipped token win is largely a lighter
payload convention, not a retrieval miracle. Second, and this is the result we
care about most, data-olympus never served a superseded rule across the whole run
(serves-stale 0.000), while plain BM25 served the retired document 75% of the time
it touched a supersession topic. Third — and this is the honest attribution — the
status-aware BM25 baseline also scores 0.000 there, which tells us the staleness
win comes from *having the status metadata*, not from our engine being a cleverer
ranker. That is the point of adding the baseline: to show which advantage is real
and where it comes from.

We also measured whether the curated `applies_when` trigger metadata earns its
keep. It does: on intents whose phrasing is covered by a trigger, it lifts recall
from 0.667 to 1.000, at roughly half the token cost of the BM25 baseline. And
because plain keyword search will always return something, even for a question
that has no governing rule at all, we added an optional abstention mode that drops
the false-positive rate on those out-of-scope queries from 1.000 to about 0.10
(a few distractors still share a real word with a rule; we report that residual
rather than round it away). For a governance tool, abstaining beats confidently
handing back a rule that does not apply.

We are equally clear about where it loses. On loosely phrased, semantic queries
that share almost no words with the authored rule, every keyword method does
poorly, and ours is no exception (recall 0.037). That is the territory where dense
or vector retrieval has a real advantage; our optional local-embedding hybrid (off
by default) closes much of it — it lifts held-out paraphrase recall from about
0.31 to about 0.53 in the governance ablation — but the default full-text stack
cannot follow a phrasing nobody wrote down, and we say so plainly rather than
hiding it.

A note on honesty: some of these numbers moved since the previous release. Fixing
a benchmark filter bug raised data-olympus exact recall from 0.858 to 1.000, and
de-leaking the synthetic corpus (it used to write the answer's lifecycle words
into the document the query searched for) removed a string-echo advantage that had
inflated the old staleness comparison. Where a number got better, it was a fixed
measurement; where the honest methodology made a claim smaller, we changed the
claim.

## Where it is going, and an invitation

This is the first version we have made public, and it is our first open source
project, so we are sharing it earlier than the comfortable instinct would suggest.
There is plenty still on our list. But we have learned that waiting for "ready" is
how things never ship, and the foundation is solid enough to build on in the open.

If any of the frustration at the top of this page sounded familiar, we would love
for you to try it, tell us where it falls short, and, if you are so inclined,
contribute. The quickstart in [`docs/quickstart.md`](docs/quickstart.md) is the
fastest way in, [`SPEC.md`](SPEC.md) is the format in full, and the issues and
discussions on the repository are open. It is Apache-2.0, and the whole knowledge
base is just your own git repository, so there is nothing to lock you in and
nothing to walk back if you try it and move on.

Thank you for reading this far. We hope it helps your team and its agents stay on
the same page. Happy vibe coding.
