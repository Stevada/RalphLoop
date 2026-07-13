# Ubiquitous Language

The vocabulary of the Ralph Loop, kept in sync with `docs/prd.md`. Where the two ever
disagree, this file is canonical.

Terms in **bold** are canonical. Anything in the "Aliases to avoid" column is banned from
prose, prompts, state names, and code identifiers.

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

Two mechanisms, kept distinct:

- **The harness hard-enforces two bounds.** The **session** — killed at the 120k ceiling
  or the wall-clock timeout, from outside, by a monitor the model cannot honour or ignore.
  And the **cycle count** — no fourth Implementer session is dispatched, whatever the Editor
  says. These are the only things the harness enforces.
- **The prompt guides one thing.** Behaviour *within* a session — "about three tries, use
  your judgment about when you are stuck." Soft, advisory, uncounted.

The 120k ceiling is on **context** — the tokens in the model's context on its most recent
call. One number for both actors, regardless of which model runs, because it measures the
model's **smart zone**, not the model's price. A model reasoning over 200k of context is a
worse engineer than the same model reasoning over 100k; the ceiling keeps every session in
the zone where its judgment is trusted. It is a *quality* bound, not a budget.

**The ceiling is not a stuck-detector, and does not count what a session spent.** Consumption
and context diverge sharply: a session re-running a failing suite twenty times may have
consumed several hundred thousand tokens while its context sits at 60k. That session is stuck,
and the thing that catches it is the **wall-clock backstop**. Consumption is recorded as
telemetry — it is what the session cost — but nothing is gated on it.

Enforcement is real time and from outside the model: Codex appends a `token_count` event to
its session rollout file after every model call, and the harness kills the process the moment
the context crosses 120k. The ceiling sits far below the model's window (272k), so it always
fires before Codex would auto-compact — compaction never gets to drop the context back under
the bound and hide the crossing.

## Work

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Parent issue** | The unit of work that yields exactly one PR against exactly one repo. | epic, story, feature |
| **Sub-issue** | An immutable node in the graph: one worktree, one Implementer, one merge. | task, ticket, issue |
| **Brief** | A sub-issue's mutable spec — acceptance criteria in prose, including its tests as prose. | description, sub-issue document |
| **Findings** | A sub-issue's mutable record of repo facts the Editor discovered in a failed worktree, carried into the next Implementer session. | guidance, advice, hints, notes |

A **Sub-issue** is fixed for the life of a run. Two fields on it are mutable, and only the
Editor writes them:

- The **Brief** says *what "done" means*. It is the thing a human diffs against the
  Planner's original intent, and the thing that softens under the spec-drift bet.
- The **Findings** say *what the last session learned* — "the client's retry logic swallows
  the expected error," "the API is really called X." Difficulty-neutral by intent: a channel
  for adding information **without** lowering the bar. Keeping them out of the brief keeps the
  brief clean as spec, and gives Editor-discovered knowledge the mandated home the
  spec-drift-3 bet regrets it lacks.

The graph's shape carries everything the runtime needs. The node blocked by nothing is
dispatched first; the node blocked by everything is dispatched last. The **Planner** reasons
about that shape in terms of contract, implementation, and integration roles when it *builds*
the graph — but those roles live in the design, not here. Once the graph exists they are
fully encoded in its `blocked by` edges, and no runtime actor reads a "kind."

A **Run** reads the graph once, at start, and never re-reads Linear during its life. The
graph is stable for the run by construction: nothing writes to its structure, because a human
intervention is what *ends* a run. Resumption is a new run against a freshly read — and
possibly replanned — graph.

## Session outcomes

The harness's classification of how *any* session ended. The model's word for its own
state; the harness's word for everything the model cannot observe about itself.

| Outcome | Detection | Routes to |
| ------- | --------- | --------- |
| `success` | Implementer: green commit, suite verified by the harness. Editor: a verdict returned. | Merge queue / act on verdict |
| `impasse` | `<impasse>` sentinel present (Implementer only) | Editor |
| `silent-red` | Session ran to completion, suite red, no sentinel (Implementer only) | Editor |
| `integration-failed` | Prospective merge conflicts or goes red after rebase onto the integration head (merge queue, not a session) | Editor |
| `ceiling-exceeded` | Context crossed the 120k **smart zone**; session killed | Human — from either actor |
| `infra-failed` | Setup failure, wall-clock timeout, rate limit, OOM (either actor) | Human — from either actor. Never the Editor. |

`impasse` and `silent-red` can only come from an Implementer session — an Editor session
cannot declare itself stuck. `ceiling-exceeded` and `infra-failed` can come from either.
`integration-failed` is not a session outcome at all: the Implementer session succeeded,
green in isolation, and the merge queue raises it when that tree will not integrate with a
sibling that landed first. It routes to the Editor on the first failure, never back to the
Implementer. The resulting Editor trip is a **cycle** like any other, counted against the
three-cycle cap.

**`ceiling-exceeded` is its own outcome, not `infra-failed`, because it says something
different.** A session whose context grew past the **smart zone** is telling you the brief was
too large to hold in a trustworthy context, or that the model wandered — a statement about the
**cut**. An `infra-failed` session is telling you the environment is broken. Both page the
human; they send that human to different places.

**It routes to the human, from either actor.** It is the one outcome that never reaches the
**Editor** — not an oversight, but the point. *"This sub-issue could not be completed inside a
trustworthy context"* is a statement about how the work was **cut**, and re-cutting is the one
thing the Editor is forbidden to do: it may rewrite a **brief**, never add, remove, or re-link
a **sub-issue**. Handed a ceiling kill, the only move available to it is to soften the brief —
which is the spec-drift failure mode, dressed up as a fix. So the sub-issue goes straight to
**needs human**, its worktree preserved. It **spends no cycle**, because no cycle occurred.

That also keeps the Editor off a bill it cannot earn back: paying Opus to explain that a
context grew too large is the same waste as paying it to diagnose `npm ci`.

**`infra-failed` pages the human too — immediately, from either actor. It is never retried,
and it never reaches the Editor.**

**There is no retry anywhere in this system.** A stale lockfile, a 429, an OOM, a wall-clock
kill: none of these are fixed by running the same session again against the same broken
environment. They are fixed by a human fixing the environment. Retrying would burn the budget,
delay the notification, and — because the failure is invisible to the model — produce a second
failure identical to the first. The honest move is to stop and say so.

An `infra-failed` session **spends no cycle**, because no **cycle** occurred: a cycle is an
Implementer session plus the Editor session that follows it, and no Editor is involved here.
The sub-issue goes straight to **needs human** with its worktree preserved, and
quarantine-and-drain does the rest — the **run** continues, and everything not downstream of it
still **lands**.

So `ceiling-exceeded` and `infra-failed` route identically: **human, no retry, no cycle, never
the Editor.** They differ only in what they tell the human — one says *the sub-issue was cut too
large*, the other says *your environment is broken.* That is a different morning, which is why
they stay distinct outcomes even though they share a destination.

## Artifacts and decisions

| Term | Definition | Aliases to avoid |
| ---- | ---------- | ---------------- |
| **Sentinel** | A fixed marker string the model prints for the harness to grep, e.g. `<impasse>`. The channel for a model's word about its own state. | flag, marker, token |
| **Impasse** | The Implementer's declaration, via the `<impasse>` sentinel, that it cannot satisfy its brief. | blocked, stuck, giving up |
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

The pre-commit hook and the merge queue's suite run **inside** the blast radius — not
because their config is mutable (though it is), but because they execute against the tree the
agent just modified, on the agent's machine, seeing its uncommitted files, hand-installed
packages, and environment. A frozen hook run against a poisoned tree still returns a poisoned
green. **CI on the PR is the only honest check**: a fresh install from the lockfile on a
clean checkout, where the hand-installed package is gone, the uncommitted stub is gone, and a
test-config edit shows up as a reviewable diff instead of silently taking effect. No malice is
implied — every green result is produced inside the blast radius of the thing being tested.

## Relationships

- A **Parent issue** contains many **Sub-issues** and produces exactly one PR.
- A **Sub-issue** has one **Brief** and one **Findings**.
- A **Cycle** is one Implementer **Session** plus one Editor **Session**; at most three.
- Every **Session** is bounded at 120k tokens; the harness also bounds cycles at three.
- An Implementer **Session** ends in a commit or an **Impasse**.
- An **Impasse** produces an **Impasse report**, which is the Editor's only sensor.
- An Editor **Session** produces one **Revision** and one **Verdict**.
- A **Sub-issue** becomes **Eligible** when every sub-issue it is blocked by has **Landed**.
- A **Parent issue** is **Done** only after a check outside the blast radius has passed.

## Example dialogue

> **Dev:** "The Implementer hit the ceiling halfway through writing tests. Is that an `impasse`?"

> **Domain expert:** "No. An **impasse** is something the Implementer *declares* through its **sentinel** — the model's word about its own state. A ceiling-killed **session** never got to say anything, so the harness classifies it `ceiling-exceeded`: the harness's word for what the model could not observe about itself. It is never retried and it never reaches the **Editor**. It goes straight to **needs human**, and it costs no **cycle**."

> **Dev:** "Why not the Editor? It diagnoses every other failure."

> **Domain expert:** "Because a context that left the **smart zone** is usually saying the sub-issue was **cut** too large — and re-cutting is the one thing the Editor may not do. It rewrites a **brief**; it never adds, removes, or re-links a **sub-issue**. Hand it a ceiling kill and the only move it has left is to soften the brief, which is the spec-drift failure wearing a fix's clothes. So a human looks at it. Paying Opus to explain that a context grew too large is the same waste as paying it to diagnose `npm ci`."

> **Dev:** "But it only used 120k of a 272k window."

> **Domain expert:** "The window is what the model *can* hold. The **smart zone** is what it can hold *well*. We would rather have a session that stopped inside the zone and told us the cut was wrong than one that ground on to 250k and produced confident nonsense."

> **Dev:** "The worktree came up with no `node_modules` and every test failed. Surely we just retry that one?"

> **Domain expert:** "No. There is **no retry anywhere in this system**. That's `infra-failed`, and it goes straight to **needs human**, same as a ceiling kill — no retry, no **cycle**, and the **Editor** never sees it. Running the session again against the same broken environment produces the same failure and spends the budget doing it. The lockfile is stale; a human fixes the lockfile."

> **Dev:** "Then why is `infra-failed` a separate outcome from `ceiling-exceeded`, if they both just page me?"

> **Domain expert:** "Because they tell you different things. One says *this sub-issue was cut too large to hold in a trustworthy context*; the other says *your environment is broken*. Same destination, completely different morning — one sends you to the graph, the other to the lockfile. The notification carries the diagnosis, and the diagnosis is the whole product."

> **Dev:** "And a session that's just *spinning* — re-running a failing suite twenty times?"

> **Domain expert:** "Flat context, so the ceiling never fires. That one is caught by the wall-clock backstop, and a wall-clock kill is `infra-failed`. The two bounds catch different failures; neither substitutes for the other."

> **Dev:** "The Editor came back `inconclusive` on 105, so it goes to **needs human**. What did it do with what it learned about the retry bug?"

> **Domain expert:** "It writes that into the sub-issue's **Findings**, not the **Brief**. The brief stays the spec — what 'done' means — so a human can still diff it against the Planner's intent. The findings carry the repo facts forward so the next session doesn't rediscover the trap. Different fields because they answer different questions."

> **Dev:** "And 107, which is blocked by 105?"

> **Domain expert:** "Nothing happens to it. It stays unstarted with its `blocked by` relation intact — it never became **eligible**, so it never got a turn. That's visibly different from 105, which tried and failed. Everything not downstream of 105 keeps going and **lands**."

> **Dev:** "Once they've all landed, the parent is **done**?"

> **Domain expert:** "Landed means it's on the **integration branch**, which every agent had write access to all run. It isn't **honest** until CI has rebuilt it from the lockfile on a checkout no agent ever touched. *Then* the parent is done."
