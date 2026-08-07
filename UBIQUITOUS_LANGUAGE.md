# Ubiquitous Language

The vocabulary of the Ralph Loop. This file is canonical for every term used across the design,
the code, and the other docs.

Terms in **bold** are canonical. Anything in the "Aliases to avoid" column is banned from
prose, prompts, state names, and code identifiers.

**This file defines terms; it does not argue the design.** A definition here is a sentence or two,
not a justification — the reasoning behind a term is recorded in the design rationale, not here.
This file points at nothing: it is where the vocabulary bottoms out.

## Actors

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Planner** | The actor that owns the shape of the work: the sub-issue graph and the first draft of every spec. | architect, decomposer |
| **Editor** | The actor that rewrites a spec and its findings after an impasse, and returns a verdict. | reviewer, mentor, critic |
| **Implementer** | The actor that writes all code and tests, working from a single spec. | worker, agent, coder |
| **Integrator** | The actor the merge queue dispatches when a sub-issue's tree will not merge: it reconciles the conflict in the worktree and commits. | merger, resolver, reconciler, conflict agent, rebaser |

## Adapters and transports

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Concrete adapter** | A vendor-specific implementation of an actor port, wired only by `cli.py`. | — |
| **Transport** | The mechanism a concrete adapter uses to carry a session, such as a subprocess or a turn-stream session. | — |
| **Turn-stream session** | A bounded stream of model text, usage observations, and auto-compaction events consumed by the role cores. | — |

## Time and bounds

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Session** | One wall-clock-bounded invocation of one actor. | round, run, invocation |
| **Cycle** | One Implementer session and the Editor session that follows it. At most three per sub-issue. | round, iteration, attempt |
| **Run** | One pass over the issue graph — read once at start, never re-read — from pre-flight to the single closing notification. | job, execution |

Two bounds and one guide, kept distinct. The terms name them:

- **The session bound** is hard, enforced from outside the model: the session is killed at the
  wall-clock backstop.
- **The cycle bound** is hard: no fourth Implementer session, whatever the Editor says.
- **"About three tries"** is soft — advisory, uncounted guidance in the prompt about behaviour
  *within* a session. It is not a bound.

## Consumption

What a session cost, in tokens. Telemetry only: nothing is gated on any of it.

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Consumed tokens** | A session's authoritative token total. | usage, spend, cost |
| **Input tokens** | Prompt tokens *not* served from cache — fresh prompt and cache writes together. | prompt tokens, request tokens |
| **Cache read tokens** | Prompt tokens served from cache. | cached tokens, cache hits |
| **Output tokens** | Tokens the model generated, reasoning included. | completion tokens, response tokens |

The three buckets are disjoint and sum to **consumed tokens**. Every concrete adapter reports the
same three, whatever its vendor calls them; a vendor that reports only a total leaves the buckets
unknown rather than zero.

## Work

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Parent issue** | The unit of work that yields exactly one PR against exactly one repo. | epic, story, feature |
| **Sub-issue** | An immutable node in the graph: one **Candidate** at a time, one Implementer, one merge. | task, ticket, issue |
| **Spec** | A sub-issue's mutable content: its full text — acceptance criteria, and everything else the Planner wrote, minus its `## Findings` section — as currently understood. | brief, description, sub-issue document |
| **Findings** | A sub-issue's mutable record of repo facts the Editor discovered in a failed worktree, carried into the next Implementer session. | guidance, advice, hints, notes |
| **Candidate** | A sub-issue's one live embodiment: the branch cut from the integration branch, the **Worktree** holding it, and the spec and findings it was cut against. | attempt, work item, submission |
| **Worktree** | The isolated git checkout a **Candidate** occupies. | working copy, work dir |

A **Sub-issue** is fixed for the life of a **Run**. Two fields on it are mutable, and only the
**Editor** writes them:

- The **Spec** says *what "done" means* — the thing a human diffs against the Planner's
  original intent. It starts as the Planner's file verbatim; the Editor may rewrite it whole
  on a `revise` verdict.
- The **Findings** say *what the last session learned about the repo* — "the client's retry
  logic swallows the expected error," "the API is really called X." Difficulty-neutral by
  intent: a channel for adding information **without** lowering the bar, kept out of the spec
  so the spec stays a clean bar, not a running commentary.

A **Candidate** is the material half of a sub-issue — the thing every unattended actor actually
holds. It is identified by its sub-issue, and a sub-issue never has two at once, so it carries no
identity of its own. Which cycle produced a given candidate is recorded on the failure report, not
on the candidate.

Its itinerary is fixed: cut from the **integration branch**, handed to the **Implementer**, and — if
that session delivers — offered to the **merge queue**, which may hand it to an **Integrator**. It
leaves in exactly one of three ways, and each of the three has a name: it **Lands**, it is
**Superseded**, or it is **Quarantined**.

The graph's shape carries everything the runtime needs: the node blocked by nothing is
dispatched first, the node blocked by everything last. The **Planner** reasons about contract,
implementation, and integration roles when it *builds* the graph, but once the graph exists
those roles are fully encoded in its `blocked by` edges — no runtime actor reads a "kind."

A **Run** reads the graph once, at start, and never re-reads it during its life. Human
intervention *ends* a run; resumption is a new run against a freshly read graph.

## Landing

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Merge queue** | The stage that serialises landing: one **Candidate** at a time takes the **merge lock**, merges the integration branch in, reconciles a conflict if it has one, runs the **suite gate**, and fast-forwards. | merge train, lander, gatekeeper |
| **Delivery** | The commits a **Candidate** carries when its Implementer session ends — what the merge queue is asked to land. | changeset, submission |
| **Prospective merge** | The tree a **Candidate** becomes once the integration branch is merged into its worktree. | merged tree, trial merge, speculative merge, premerge |
| **Suite gate** | The merge queue's run of the repo's `test` command on the **prospective merge**. | pre-merge check, merge test, gate run |

The **suite gate** is the only suite run that decides a landing, and it runs on the **prospective
merge** — never on the candidate's own tree, never in the base checkout.

## Commands

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Command descriptor** | The `.ralph.toml` file at the target repo root. Its `[commands]` table declares the repo's runnable commands for Ralph. | command flags, CLI command config, `--test-cmd`, `--install-cmd` |
| **RepoCommands** | The value Ralph passes after command discovery: a required already-split `test` command and an optional already-split `install` command. | command config, command map, raw command strings |
| **Command discovery** | The pre-flight step that asks a **CommandSource** for **RepoCommands** from the target repo. | command guessing, command inference, LLM discovery |
| **CommandSource** | The port that discovers **RepoCommands** for a target repo. | command resolver, command provider, test command flag |

## Pre-flight

The **pre-flight** is the gate a **Run** passes before it opens any session: it **refuses**, it does
not warn, and it returns every **refusal**, not the first. Each refusal names one of seven checks —
the condition it found, never merely that the run cannot start.

| Check | What it names |
| ----- | ------------- |
| `protected-branch` | HEAD is on a branch the run would fast-forward, e.g. `main`. |
| `uncommitted-changes` | Tracked changes in the working tree the merge queue would fight. |
| `uninstalled-pre-commit-hooks` | The repo configures pre-commit, but no hook is installed. |
| `missing-test-command` | Command discovery found no runnable `test` command. |
| `invalid-issue-source` | The issue source could not be read — unreachable or misconfigured. |
| `invalid-issue-graph` | The source read, but its graph is malformed — a cycle, or a spec with no acceptance criteria. |
| `missing-actor-runtime` | A named actor's runtime is not installed, so its sessions could never open. |

## Session outcomes

The harness's classification of how *any* session ended. The model's word for its own
state; the harness's word for everything the model cannot observe about itself.

| Outcome | Detection | Routes to |
| ------- | --------- | --------- |
| `success` | Implementer: committed delivery ready for the merge queue. Editor: a verdict returned. Integrator: the merge is committed and no conflict remains. | Merge queue / act on verdict / the suite gate |
| `impasse` | The Implementer did not deliver: the `<impasse>` sentinel or no commits. | Editor |
| `integration-failed` | Prospective merge conflicts or goes red after the integration branch is merged in (merge queue, not a session) | Editor, or the Integrator on a conflict |
| `infra-failed` | Setup failure, wall-clock timeout, rate limit, OOM (any actor) | Human — from any actor. Never the Editor. |

`impasse` can only come from an Implementer session — an Editor cannot fail to deliver a spec
it was never given. `infra-failed` can come from any actor. `integration-failed` is not a
session outcome at all: the Implementer committed work that reached the merge queue, and the merge
queue raises it when that tree will not integrate with a sibling that landed first.

The two ways to raise it are answered by different actors. A prospective merge that goes **red**
routes to the **Editor**, and counts as a **cycle**. A prospective merge that **conflicts** is
answered by **conflict reconciliation** inside the merge queue, spends no **cycle**, and never
reaches the Editor: a conflict is not evidence that a spec is wrong.

`infra-failed` routes to the human — **no retry, no cycle, never the Editor**.

## Artifacts and decisions

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Sentinel** | A fixed marker string the model prints for the harness to grep, e.g. `<impasse>`. The channel for a model's word about its own state. | flag, marker, token |
| **Impasse** | The outcome of an Implementer session that could not satisfy its spec — whether the model **declared** it via the `<impasse>` sentinel, or the harness **caught** it undeclared by observing no commits. | blocked, stuck, giving up, silent-red |
| **Impasse report** | The Implementer's structured exit artifact, corroborated by harness-supplied facts. | blocker report, failure report |
| **Conflict reconciliation** | What the merge queue does instead of giving up on a conflict: it merges the integration branch into the worktree, dispatches an **Integrator** to resolve and commit, and runs its suite gate on the result. No new spec, no revision, and it spends no **cycle**. | retry, re-run, second attempt, rebase-conflict recovery |
| **Quarantine** | What the harness does with a **Candidate** it cannot land: preserve its worktree, put the sub-issue in **needs human**, and page at the end of the run. | park, shelve, sideline |
| **Superseded** | The end of a **Candidate** whose Editor returned `revise`: its worktree and branch are both destroyed, and the sub-issue is cut a fresh candidate against the rewritten spec. | retry, restart, rollback |
| **Revision** | The Editor's rewrite of a spec (and its findings), recorded alongside the Planner's original rather than over it. | edit, fix, update |
| **Verdict** | The Editor's decision when its session succeeds: `revise`, `planning-defect`, or `inconclusive`. | outcome, ruling, judgment |
| **Run log** | An append-only file, one line per event, recording session states and Editor verdicts for a run — nothing heavier. | trace, audit log, journal |

**Blocked** is reserved for Linear's native `blocked by` issue relation, and means only
"has an unsatisfied dependency edge." It never describes an actor's or a session's state.

### Verdicts

| Verdict | The Editor is saying | Effect |
| ------- | -------------------- | ------ |
| `revise` | "The spec was wrong and I have fixed it." | Implementer restarts clean. |
| `planning-defect` | "The cut is wrong. This sub-issue should not exist in this shape." | Quarantine; page the human. |
| `inconclusive` | "I have spent my cycles and I cannot tell you why this will not land." | Quarantine; page the human. |

`planning-defect` and `inconclusive` both quarantine and both page. They differ in
diagnosis, which is the only thing the notification carries and the only thing that decides
whether the human's first ten minutes go to a diff or to a PRD.

**The cycle cap forces a terminal verdict.** The third Editor session may not return
`revise` — the harness will not dispatch a fourth Implementer session, so the third verdict
must be `planning-defect` or `inconclusive`.

## Lifecycle

Sub-issue states map onto Linear state *types*; the dependency graph maps onto Linear's
native `blocked by` relation.

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Eligible** | Derived, not stored: every sub-issue this one is blocked by has landed. | ready, unblocked |
| **Ready** | Stored: the Planner has authorised this sub-issue to run. | eligible, queued |
| **Landed** | A sub-issue's terminal state: fast-forwarded into the integration branch. | done, merged, complete |
| **Done** | A parent issue's terminal state: PR merged, CI green, human has read the diff. | landed, shipped, closed |
| **Needs human** | A sub-issue's quarantine state, following `planning-defect`, `inconclusive`, or a conflict the Integrator could not land. Its worktree is preserved. | blocked, blocked-human, escalated |

There is no state for a sub-issue whose upstream escalated. It is unstarted, and it carries
a `blocked by` relation to something that never landed. "I failed" and "I never got a turn"
are already distinguishable without inventing a state for the second one.

## Trust

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Blast radius** | Everything an actor could have affected during its session: the working tree *and* uncommitted files, installed packages, environment, `.env`. | sandbox, scope |
| **Honest** | Of a result: produced outside the blast radius of the actor that produced the code. | verified, trusted, green |
| **Integration branch** | The branch sub-issues land on. Inside the blast radius. | trunk, main, base |
| **Merge lock** | The mutex a worktree holds while it merges the integration branch in, reconciles a conflict if it has one, re-runs the suite, and fast-forwards. | integration lock, queue lock |

The pre-commit hook and the merge queue's suite run **inside** the blast radius: they execute
against the tree the actor just modified, on the actor's machine. **CI on the PR is the only honest
check** — a fresh install from the lockfile on a checkout no actor touched. No malice is implied;
every green result is produced inside the blast radius of the thing being tested.

## Relationships

- A **Parent issue** contains many **Sub-issues** and produces exactly one PR.
- A **Sub-issue** has one **Spec** and one **Findings**.
- A **Sub-issue** has at most one live **Candidate**, and at most three over a **Run**.
- A **Candidate** occupies one **Worktree** and carries one **Delivery** into the **Merge queue**.
- The **Merge queue** admits one **Candidate** at a time, and runs the **Suite gate** on its
  **Prospective merge**.
- A **Candidate** ends **Landed**, **Superseded**, or **Quarantined** — exactly one of the three,
  and always one.
- A **Cycle** is one Implementer **Session** plus one Editor **Session**; at most three.
- Every **Session** is bounded by wall clock; the harness also bounds cycles at three.
- An Implementer **Session** ends in a green **Delivery** or an **Impasse**.
- An **Impasse** produces the report that is the Editor's only sensor.
- An Editor **Session** produces one **Revision** and one **Verdict**.
- An `integration-failed` raised by a conflict is answered by **Conflict reconciliation** and never
  reaches the **Editor**; a red one goes to the **Editor** and spends a **Cycle**.
- A **Conflict reconciliation** ends in one of two ways: the sub-issue **Lands**, or it **Needs
  human**. It never returns to the merge queue.
- A **Sub-issue** becomes **Eligible** when every sub-issue it is blocked by has **Landed**.
- A **Parent issue** is **Done** only after a check outside the blast radius has passed.
