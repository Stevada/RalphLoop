# Ubiquitous Language

The vocabulary of the Ralph Loop. This file is canonical for every term used across the design,
the code, and the other docs.

Terms in **bold** are canonical. Anything in the "Aliases to avoid" column is banned from
prose, prompts, state names, and code identifiers.

**This file defines terms; it does not argue the design.** A definition here is a sentence or two,
not a justification — the reasoning behind a term (why the ceiling is on context, why nothing is
ever retried) is recorded in the design rationale, not here. This file points at nothing: it is
where the vocabulary bottoms out.

## Actors

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Planner** | The actor that owns the shape of the work: the sub-issue graph and the first draft of every brief. | architect, decomposer |
| **Editor** | The actor that rewrites a brief and its findings after an impasse, and returns a verdict. | reviewer, mentor, critic |
| **Implementer** | The actor that writes all code and tests, working from a single brief. | worker, agent, coder |

## Time and bounds

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Session** | One bounded invocation of one actor: one process, one 120k-token **context** ceiling, one wall-clock backstop. | round, run, invocation |
| **Smart zone** | The context range in which a model's judgment is reliable — 120k tokens. The **ceiling** exists to keep every session inside it. | context limit, budget, quota |
| **Cycle** | One Implementer session and the Editor session that follows it. At most three per sub-issue. | round, iteration, attempt |
| **Run** | One pass over the issue graph — read once at start, never re-read — from base-green check to the single closing notification. | job, execution |

Two bounds and one guide, kept distinct. The terms name them:

- **The session bound** is hard, enforced from outside the model: the session is killed at the
  120k **context** ceiling or the wall-clock backstop. The ceiling reads **context**, never
  **consumption**, and measures the **smart zone** rather than cost — one number for both
  actors, whichever model runs. The wall-clock backstop is what catches a *stuck* session,
  whose context stays flat while it spins.
- **The cycle bound** is hard: no fourth Implementer session, whatever the Editor says.
- **"About three tries"** is soft — advisory, uncounted guidance in the prompt about behaviour
  *within* a session. It is not a bound.

## Work

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Parent issue** | The unit of work that yields exactly one PR against exactly one repo. | epic, story, feature |
| **Sub-issue** | An immutable node in the graph: one worktree, one Implementer, one merge. | task, ticket, issue |
| **Brief** | A sub-issue's mutable spec — acceptance criteria in prose, including its tests as prose. | description, sub-issue document |
| **Findings** | A sub-issue's mutable record of repo facts the Editor discovered in a failed worktree, carried into the next Implementer session. | guidance, advice, hints, notes |

A **Sub-issue** is fixed for the life of a **Run**. Two fields on it are mutable, and only the
**Editor** writes them:

- The **Brief** says *what "done" means* — the thing a human diffs against the Planner's
  original intent.
- The **Findings** say *what the last session learned about the repo* — "the client's retry
  logic swallows the expected error," "the API is really called X." Difficulty-neutral by
  intent: a channel for adding information **without** lowering the bar, kept out of the brief
  so the brief stays clean as spec.

The graph's shape carries everything the runtime needs: the node blocked by nothing is
dispatched first, the node blocked by everything last. The **Planner** reasons about contract,
implementation, and integration roles when it *builds* the graph, but once the graph exists
those roles are fully encoded in its `blocked by` edges — no runtime actor reads a "kind."

A **Run** reads the graph once, at start, and never re-reads it during its life. Human
intervention *ends* a run; resumption is a new run against a freshly read graph.

## Pre-flight

The **pre-flight** is the gate a **Run** passes before it opens any session: it **refuses**, it does
not warn, and it returns every **refusal**, not the first. Each refusal names one of five checks — the
condition it found, never merely that the run cannot start.

| Check | What it names |
| ----- | ------------- |
| `protected-branch` | HEAD is on a branch the run would fast-forward, e.g. `main`. |
| `uncommitted-changes` | Tracked changes in the working tree the merge queue would fight. |
| `uninstalled-pre-commit-hooks` | The repo configures pre-commit, but no hook is installed. |
| `invalid-issue-source` | The issue source could not be read — unreachable or misconfigured. |
| `invalid-issue-graph` | The source read, but its graph is malformed — a cycle, or a brief with no acceptance criteria. |

## Session outcomes

The harness's classification of how *any* session ended. The model's word for its own
state; the harness's word for everything the model cannot observe about itself.

| Outcome | Detection | Routes to |
| ------- | --------- | --------- |
| `success` | Implementer: green commit, suite verified by the harness. Editor: a verdict returned. | Merge queue / act on verdict |
| `impasse` | The Implementer did not deliver: the `<impasse>` sentinel, no commits, or a red suite (Implementer only) | Editor |
| `integration-failed` | Prospective merge conflicts or goes red after rebase onto the integration head (merge queue, not a session) | Editor |
| `ceiling-exceeded` | Context crossed the 120k **smart zone**; session killed | Human — from either actor |
| `infra-failed` | Setup failure, wall-clock timeout, rate limit, OOM (either actor) | Human — from either actor. Never the Editor. |

`impasse` can only come from an Implementer session — an Editor cannot fail to deliver a brief
it was never given. `ceiling-exceeded` and `infra-failed` can come from either actor.
`integration-failed` is not a session outcome at all: the Implementer session succeeded green
in isolation, and the merge queue raises it when that tree will not integrate with a sibling
that landed first. It routes to the Editor on the first failure and counts as a **cycle** like
any other.

`ceiling-exceeded` and `infra-failed` route identically — **human, no retry, no cycle, never
the Editor** — and differ only in what they tell the human: *the sub-issue was cut too large*
versus *your environment is broken*.

## Artifacts and decisions

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Sentinel** | A fixed marker string the model prints for the harness to grep, e.g. `<impasse>`. The channel for a model's word about its own state. | flag, marker, token |
| **Impasse** | The outcome of an Implementer session that could not satisfy its brief — whether the model **declared** it via the `<impasse>` sentinel, or the suite **caught** it undeclared (no commits, or a red suite). | blocked, stuck, giving up, silent-red |
| **Impasse report** | The Implementer's structured exit artifact, corroborated by harness-supplied facts. | blocker report, failure report |
| **Revision** | The Editor's rewrite of a brief (and its findings), recorded alongside the Planner's original rather than over it. | edit, fix, update |
| **Verdict** | The Editor's decision when its session succeeds: `revise`, `planning-defect`, or `inconclusive`. | outcome, ruling, judgment |
| **Run log** | An append-only file, one line per event, recording session states and Editor verdicts for a run — nothing heavier. | trace, audit log, journal |

**Blocked** is reserved for Linear's native `blocked by` issue relation, and means only
"has an unsatisfied dependency edge." It never describes an actor's or a session's state.

### Verdicts

| Verdict | The Editor is saying | Effect |
| ------- | -------------------- | ------ |
| `revise` | "The brief was wrong and I have fixed it." | Implementer restarts clean. |
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
| **Needs human** | A sub-issue's quarantine state, following `planning-defect` or `inconclusive`. Its worktree is preserved. | blocked, blocked-human, escalated |

There is no state for a sub-issue whose upstream escalated. It is unstarted, and it carries
a `blocked by` relation to something that never landed. "I failed" and "I never got a turn"
are already distinguishable without inventing a state for the second one.

## Trust

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Blast radius** | Everything an actor could have affected during its session: the working tree *and* uncommitted files, installed packages, environment, `.env`. | sandbox, scope |
| **Honest** | Of a result: produced outside the blast radius of the actor that produced the code. | verified, trusted, green |
| **Integration branch** | The branch sub-issues land on. Inside the blast radius. | trunk, main, base |
| **Merge lock** | The mutex a worktree holds while it rebases, re-runs the suite, and fast-forwards. | integration lock, queue lock |

The pre-commit hook and the merge queue's suite run **inside** the blast radius: they execute
against the tree the agent just modified, on the agent's machine. **CI on the PR is the only
honest check** — a fresh install from the lockfile on a checkout no agent touched. No malice is
implied; every green result is produced inside the blast radius of the thing being tested.

## Relationships

- A **Parent issue** contains many **Sub-issues** and produces exactly one PR.
- A **Sub-issue** has one **Brief** and one **Findings**.
- A **Cycle** is one Implementer **Session** plus one Editor **Session**; at most three.
- Every **Session** is bounded at 120k tokens; the harness also bounds cycles at three.
- An Implementer **Session** ends in a green delivery or an **Impasse**.
- An **Impasse** produces the report that is the Editor's only sensor.
- An Editor **Session** produces one **Revision** and one **Verdict**.
- A **Sub-issue** becomes **Eligible** when every sub-issue it is blocked by has **Landed**.
- A **Parent issue** is **Done** only after a check outside the blast radius has passed.
