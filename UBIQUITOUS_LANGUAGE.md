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
| **Session** | One bounded invocation of one actor: one process, one 120k-token ceiling, one wall-clock backstop. | round, run, invocation |
| **Cycle** | One Implementer session and the Editor session that follows it. At most three per sub-issue. | round, iteration, attempt |
| **Run** | One pass over the issue graph — read once at start, never re-read — from base-green check to the single closing notification. | job, execution |

Two mechanisms, kept distinct:

- **The harness hard-enforces two bounds.** The **session** — killed at the 120k ceiling
  or the wall-clock timeout, from outside, by a monitor the model cannot honour or ignore.
  And the **cycle count** — no fourth Implementer session is dispatched, whatever the Editor
  says. These are the only things the harness enforces.
- **The prompt guides one thing.** Behaviour *within* a session — "about three tries, use
  your judgment about when you are stuck." Soft, advisory, uncounted.

The 120k ceiling counts **tokens consumed**, nothing else — one number for both actors,
regardless of which model runs. It is a stuck-detector: it catches a session spinning (a
suite re-run twenty times), not a budget. Note that consumption diverges from context — a
session whose context window sits at 60k may have consumed several hundred thousand tokens
re-running a failing suite. The ceiling is on consumption.

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
| `ceiling-exceeded` | Cumulative token consumption crossed 120k; session killed | Implementer → Editor; Editor → human |
| `infra-failed` | Setup failure, wall-clock timeout, rate limit, OOM (either actor) | Retry with backoff, then human |

`impasse` and `silent-red` can only come from an Implementer session — an Editor session
cannot declare itself stuck. `ceiling-exceeded` and `infra-failed` can come from either.
`integration-failed` is not a session outcome at all: the Implementer session succeeded,
green in isolation, and the merge queue raises it when that tree will not integrate with a
sibling that landed first. It routes to the Editor on the first failure, never back to the
Implementer. The resulting Editor trip is a **cycle** like any other, counted against the
three-cycle cap.

**`ceiling-exceeded` is its own outcome, not `infra-failed`, because it is not transient.**
Retrying `npm ci` may clear an infra failure; retrying a session that spent 120k without
finishing just spends another 120k the same way. So a ceiling kill is **never retried**. An
Implementer that hits it routes to the **Editor**, which diagnoses from the partial worktree
and the harness's facts — final test output, diffstat, token spend — *without* a fabricated
impasse report. An Editor that hits it has no diagnostician above it, so it pages the human
directly. The three-cycle cap bounds any loop in which the ceiling recurs.

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

> **Domain expert:** "No. An **impasse** is something the Implementer *declares* through its **sentinel** — the model's word about its own state. A ceiling-killed **session** never got to say anything, so the harness classifies it `infra-failed` and retries it. It never reaches the **Editor**, because paying Opus to diagnose a token ceiling is the waste the taxonomy exists to prevent."

> **Dev:** "The Editor came back `inconclusive` on 105, so it goes to **needs human**. What did it do with what it learned about the retry bug?"

> **Domain expert:** "It writes that into the sub-issue's **Findings**, not the **Brief**. The brief stays the spec — what 'done' means — so a human can still diff it against the Planner's intent. The findings carry the repo facts forward so the next session doesn't rediscover the trap. Different fields because they answer different questions."

> **Dev:** "And 107, which is blocked by 105?"

> **Domain expert:** "Nothing happens to it. It stays unstarted with its `blocked by` relation intact — it never became **eligible**, so it never got a turn. That's visibly different from 105, which tried and failed. Everything not downstream of 105 keeps going and **lands**."

> **Dev:** "Once they've all landed, the parent is **done**?"

> **Domain expert:** "Landed means it's on the **integration branch**, which every agent had write access to all run. It isn't **honest** until CI has rebuilt it from the lockfile on a checkout no agent ever touched. *Then* the parent is done."
