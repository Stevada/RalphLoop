# Ralph Loop — Design rationale

Harnessed engineering for coding agents. Three actors, one medium, one repo, one PR.

This is the **why**: the design and the reasoning behind it, including the risks accepted
deliberately. The first implementation has been built against it — where the code and this document
disagree, the code is the fact and this is the intent it was meant to serve.

Vocabulary is canonical in [`UBIQUITOUS_LANGUAGE.md`](../UBIQUITOUS_LANGUAGE.md); this document uses
those terms and does not define them.

---

## 1. Actors

Three actors. They never talk to each other. They talk to a sub-issue's **brief** and
**findings**.

| Actor | Backed by | Writes | Reads |
|---|---|---|---|
| **Planner** | Claude Opus, in conversation | The issue graph and the first draft of every brief | PRD, codebase, integration branch |
| **Editor** | Claude Code **or** Copilot | A single sub-issue's brief and findings | Everything; may run read-only commands |
| **Implementer** | Codex **or** Copilot | All code, tests included | Its brief and findings, the repo |

The Planner is invoked by a human, in conversation. The Editor and Implementer run
unattended inside a run.

**An actor is a role, not a model.** Either CLI can back either of the two unattended roles;
`cli.py` chooses from CLI arguments, and nothing downstream knows which is running. That is a
portfolio decision, not a hedge: the Implementer and the Editor should
not be the same model on the same failure, because an Editor adjudicating an impasse declared
by *itself* is the least independent sensor the system could have.

---

## 2. Invariants

These are load-bearing. Violating any of them collapses a boundary drawn elsewhere.

1. **A sub-issue's brief and findings are the only channel between actors.**
2. **Structure is immutable within a run.** The Planner's graph — sub-issues and their
   blocking edges — is fixed when the run reads it at start. The Editor may rewrite a brief
   and its findings; it may never add, remove, or re-link a sub-issue.
3. **A run reads the issue graph exactly once, at start.** No mid-run reads. Human
   intervention ends the run; resumption is a new run that re-reads the graph.
4. **The Editor writes only the brief and findings.** It may read anything and run read-only
   commands. It never commits, never cherry-picks, never touches a worktree except to
   read it. The moment the Editor commits, it is an Implementer with a different name.
5. **Nothing enters the integration branch unverified.** Therefore nothing after
   integration needs verification.
6. **Linear is the source of truth for the plan. Git is where the work lives.**
   Write-through, read-once: the run writes to Linear as it goes and never reads back.

---

## 3. Shape of a parent issue

One repo. One Kanban. One parent issue. One PR.

```
                 ┌──────────────────┐
                 │ contract         │   interfaces, stubs, schema
                 │ sub-issue        │   (no tests — tests are prose in each brief)
                 └────────┬─────────┘
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │ impl 1   │    │ impl 2   │    │ impl 3   │   parallel, isolated worktrees
    └────┬─────┘    └────┬─────┘    └────┬─────┘
         └───────────────┼───────────────┘
                         ▼
                 ┌──────────────────┐
                 │ integration      │   one honest end-to-end test, zero mocks
                 │ sub-issue        │
                 └──────────────────┘
```

**The contract sub-issue exists because parallel Implementers cannot see each other.**
Without a landed interface, each one invents its own boundary and mocks across it — and
TDD actively rewards this, since a mock is the cheapest way to turn a red test green when
the real dependency does not exist yet. Three sub-issues each mocking auth produce three
green branches and an integration branch where nothing can authenticate. The contract
wave makes drift a **typecheck** failure, caught in the worktree, cheaply.

**The integration sub-issue exists because typechecking does not catch behavioural
drift.** It is blocked by every implementation sub-issue and forbidden from mocking.

The contract sub-issue is the highest-leverage artifact in the system. A wrong contract
means N Implementers build correctly against a wrong interface, all tests pass, and the
integration sub-issue fails — at which point no amount of Editor cycles can help, because
the defect is upstream. This is what `planning-defect` exists for.

---

## 4. Lifecycle of a run

### 4.1 Read the graph once

The run reads the Linear issue graph once, at start, and works from that read for its whole
life. Everything the Implementer and Editor read derives from it. Nothing reads Linear again.

### 4.2 Base green check

Before any agent starts, run the repo's test command on the integration branch.

**If it is red, no agent starts.** This single check catches the stale lockfile, the
broken environment, the half-merged previous run, and the case where trunk was already
broken. It also gives every subsequent failure a meaningful baseline: *these tests passed
twenty minutes ago on this exact tree.*

Without it, every `<impasse>` is ambiguous, and the Editor — the most expensive actor in
the system — reasons from a baseline it cannot verify.

### 4.3 Dispatch

A sub-issue is **eligible** when every sub-issue it is blocked by has `landed`. Eligible
sub-issues are dispatched concurrently, each into its own git worktree branched from the current
integration head.

There is **no wave barrier.** Waves are an artifact of dependencies, not of merging. A
fast sub-issue lands in three minutes rather than waiting for its slowest sibling.

Dependencies are installed **once, in the base checkout**, before any worktree exists.
Installation failure is loud and fatal — never `|| true`.

### 4.4 The Implementer's session

One continuous `codex exec` session. It writes tests first, then implementation,
per `/tdd`. It iterates as it sees fit — running the suite, fixing, trying again — using its
own judgment about when it is stuck.

That inner loop is the *model's*, inside one session, and is the only thing in this system that
resembles a retry. **The harness never retries anything**: it never re-dispatches a session, and
it has no backoff. Do not read the sentence above as licence to add one.

It exits exactly one of two ways:

- **Green**, with a commit, and it proceeds to the merge queue.
- **`<impasse>`**, with a structured impasse report.

"About three tries" is **guidance in the prompt**, not a harness-enforced counter. The
harness counts nothing but tokens.

#### The impasse report

The Editor's only sensor. It is a defendant's statement, so the harness corroborates it.

The Implementer supplies: the failing test and its assertion output verbatim; the
approaches tried and why each was abandoned; the specific acceptance criterion it believes
unsatisfiable; what would make it satisfiable.

The harness appends, independently of the model's narration: final test output, diffstat,
files touched, wall-clock, context high-water mark, tokens consumed.

**The model's story, checked against the harness's facts.** Those two disagreeing is
itself a signal worth surfacing.

#### The only hard bound

**120,000 tokens of context.** Wall-clock timeout as a backstop.

**The ceiling is on context, not on consumption.** 120k is the size of the model's *smart
zone* — the region where its judgment is reliable. It is not a budget, not a share of the
window, and not a count of what the session spent. A model reasoning over 200k of context is
a worse engineer than the same model reasoning over 100k, and this ceiling exists to keep
every session inside the zone where we trust it. Cost is not the argument; **quality** is.

Crossing the ceiling kills the session, with the outcome `ceiling-exceeded` — a distinct
outcome, never `infra-failed`, because it is not transient. A session whose context grew past
the smart zone is not going to need less context on a retry: either the brief is too large to
hold in a trustworthy context, or the model wandered. Either way the answer is not to run it
again, so it is **never retried**.

**It pages the human, from either actor. It is the one outcome that never reaches the Editor.**

That is not an oversight; it is the whole shape of the thing. *"This sub-issue could not be
completed inside a trustworthy context"* is a statement about how the work was **cut** — and
re-cutting is precisely what the Editor is forbidden to do. Invariant 2: it may rewrite a brief,
never add, remove, or re-link a sub-issue. Hand it a ceiling kill and the only move left to it
is to soften the brief until the work fits, which is bet #2's failure mode arriving dressed as
a remedy. The sub-issue goes to `needs-human` with its worktree preserved, and it **spends no
cycle**, because no cycle occurred.

It also keeps the Editor off a bill it cannot earn back. Paying Opus to explain that a context
grew too large is the same waste as paying it to diagnose `npm ci` — the waste this taxonomy
exists to prevent.

##### Enforcement

Real time, from outside the model. `codex exec --json` on stdout does **not** stream usage —
one `exec` invocation is a single turn and its `turn.completed` event lands only at the end.
But Codex appends a `token_count` event to its session rollout file
(`~/.codex/sessions/<date>/rollout-*.jsonl`) after **every model call**, carrying:

```
info.last_token_usage.input_tokens   → the context on the most recent call   ← the ceiling
info.total_token_usage.total_tokens  → cumulative consumption                ← telemetry only
info.model_context_window            → 272,000 for gpt-5.5
rate_limits.primary.used_percent     → free early warning for the 429 → infra-failed case
```

The harness tails that file and kills the process the moment `last_token_usage.input_tokens`
crosses 120k. Note the ceiling sits far below the 272k window, so it always fires **before**
Codex would auto-compact — compaction never gets the chance to silently drop the context back
under the bound and hide the crossing.

Consumption is recorded as telemetry — it is what the session cost — but nothing is gated on
it. A session that re-runs a failing suite twenty times may consume several hundred thousand
tokens while its context sits at 60k; that is a stuck session, and the thing that catches it
is the **wall-clock timeout**, not the ceiling. The two bounds catch different failures and
neither substitutes for the other.

The 120k figure is a starting guess at where the smart zone ends. Instrument real runs, look
at the distribution of context high-water marks against outcomes, then tune.

### 4.5 The merge queue

This is the piece that makes parallelism honest, and the piece Ralph most conspicuously
lacks today.

When an Implementer reaches green in its own worktree:

1. **Acquire the merge lock.**
2. **Rebase** onto the current integration head.
3. **Re-run the suite in its own worktree.**
4. On green: **fast-forward merge**, then write `landed` to Linear.
5. **Release the lock.**

On rebase conflict or red suite on the prospective merge: **release the lock, preserve the
worktree, and route the sub-issue to the Editor** as an `integration-failed` outcome. The
Implementer does not retry. A failed merge is not a transient hiccup to paper over — it is
a signal about how the work was cut, and adjudicating it is the Editor's job, not something
the actor that just failed to integrate should be trusted to fix by trying again.

The lock is held only for rebase, suite, fast-forward, release — never while the Editor
reasons. One sub-issue's integration failure never stalls the queue for its siblings.

Because the suite runs on the *prospective* merge result, `git merge` in the harness is
only ever a fast-forward of an already-verified tree. The integration branch is correct by
construction.

#### Why the actor, not bash

`git merge` does not fire the `pre-commit` hook. A clean merge creates its commit without
invoking it. So per-branch hooks guarantee **each branch is green in isolation** — a
strictly weaker claim than "the merged tree is green," and the gap between those two
claims is exactly where semantic conflicts live:

> Sub-issue 104 renames an export. Sub-issue 105 imports the old name. Different files.
> No textual conflict. Both branches green. Merged tree: red.

If bash performed the merge, the break would surface waves later — no worktree, no context,
no attribution. Under the merge queue the failure is caught the moment it happens, on the
prospective merge against the exact sibling that landed first, and the **worktree is
preserved** for the Editor. A live actor with the failing tree in front of it decides
whether 105's brief should adapt to 104's rename or whether the two were badly cut — a
decision bash could never make, and one the already-exited Implementer is no longer around
to make either.

#### A failed merge is an Editor trigger, not a retry loop

A conflict or red suite on the prospective merge routes the sub-issue to the Editor as
`integration-failed` — immediately, on the first failure, with no Implementer requeue. The
Editor reads the preserved worktree and the sibling that landed first. If 105 can adapt to
104, it says `revise` and the Implementer restarts clean; if *"105 cannot land alongside
104"* because the two were badly cut, it has exactly the evidence for `planning-defect`.

That trip through the Editor spends one of the sub-issue's three **cycles** — an
`integration-failed` is counted exactly like an impasse, so a sub-issue that keeps failing
to integrate is escalated at the third.

The merge queue itself does no model work: rebase and the suite run are mechanical, so a
sub-issue spends no tokens to land work it has already finished. Only the Editor, on an
integration failure, costs anything.

#### The merge queue as an instrument

A parent issue whose sub-issues sail through the queue was decomposed well. One whose
sub-issues fail to integrate was not — and the merge queue turns that into `integration-failed`
routes and `planning-defect` verdicts from the Editor, rather than mysterious failures three
waves later.

Decomposition quality has no other automated check in this system. This is it.

### 4.6 The Editor's session

Triggered by an `impasse` — declared or not — or an `integration-failed` merge. Never by
`infra-failed`, and never by `ceiling-exceeded` — those two are the outcomes the Editor
cannot help with, one because it is transient and one because its remedy is a re-cut the
Editor is forbidden to make.

The Editor:

- reads the brief, the findings, the failure report (an Implementer impasse report, or — for
  `integration-failed` — the harness's record, since the Implementer authored none), and the
  **failed worktree**;
- **runs read-only commands** — re-runs the suite, greps, checks whether the API the
  Implementer complained about actually exists;
- rewrites the brief and records findings;
- returns a verdict.

Reproduction, not inference. The difference between *"this cannot be done as specified"*
and *"the Implementer gave up early"* is often a single grep. Since declaring an impasse
is cheap, the Editor's ability to check the Implementer's story against the repository is
the only thing standing between us and a system where declaring an impasse always works.

**On Editor entry, the Implementer's work is discarded.** It restarts clean against the
revised brief. No code survives. Knowledge survives only if the Editor writes it into the
findings — that is the Editor's judgment, unmandated.

Verdicts:

| Verdict | Effect |
|---|---|
| `revise` | Brief and findings rewritten; Implementer restarts clean. |
| `planning-defect` | Sub-issue quarantined; **always pages the human.** |
| `inconclusive` | Sub-issue quarantined; pages the human. |

At most **three cycles** per sub-issue, hard-enforced: the harness dispatches no fourth
Implementer session, so the third Editor session must return a terminal verdict —
`planning-defect` or `inconclusive`, never `revise`.

### 4.7 Quarantine and drain

When a sub-issue escalates, the run does **not** stop.

- The sub-issue is marked `needs-human`. Its worktree is preserved.
- Everything transitively blocked by it never becomes eligible, and is skipped.
- Every unaffected sub-issue **continues and lands**.
- The run ends with **one** notification.

A system that pages you the instant the first thing goes wrong trains you to ignore it.
You want a complete picture of the run at 8am, not an alert at 3am.

A skipped sub-issue needs no failure state of its own: it stays unstarted with its
`blocked by` edge intact, which is already distinct from `needs-human`. The next run can
tell *"I failed"* from *"I never got a turn"* without inventing a state for the second.

### 4.8 Escalation to the human

Linear carries the content; Linear's own notifications are the doorbell. No Slack
integration to build or own.

One notification per run. It states: which parent issue; how many sub-issues landed; how
many failed to land and why; which sub-issue needs attention first; and the verdict that
caused it — `inconclusive` after three cycles is a very different morning from
`planning-defect` on cycle one. It links to the preserved worktree, the integration
branch, and the Linear issue.

**If you cannot tell from the notification alone whether to spend your first ten minutes
reading a diff or rewriting a PRD, the notification has failed.**

### 4.9 Replanning

`planning-defect` always pages the human. The Planner is never invoked headless.

Structure is the one artifact in this system with **no error-correcting feedback**. The
Editor corrects the Implementer. The tests correct the Implementer. Nothing corrects the
Planner but a human. Auto-applying the Planner's self-revision would close the only loop
with a human in it, precisely at the moment the system has just proven the Planner wrong.

Replanning is **replan-the-remainder**: the Planner is handed the integration branch and
the set of already-merged sub-issues, and plans the rest. Successful work is never thrown
away to preserve planning purity.

> From the second run onward, the Planner is planning against a codebase that agents wrote,
> and the PRD is only half the input. The Planner's prompt must know this.

**A parent issue that receives a second `planning-defect` in its lifetime escalates
unconditionally.** Two Editors, on two different sub-issues, independently concluding the
decomposition is wrong is not a spec problem. It is a signal that the PRD is wrong, and no
amount of replanning fixes a bad PRD.

---

## 5. The failure taxonomy

The harness owns **four** failure outcomes, not one. This is the single highest-value piece
of harness logic.

| Outcome | Detection | Routes to |
|---|---|---|
| `impasse` | Session did not deliver: the `<impasse>` sentinel, or no commits, or a red suite | **Editor** |
| `integration-failed` | Prospective merge conflicts, or the suite is red after rebase onto the integration head | **Editor** |
| `ceiling-exceeded` | Context crossed the 120k smart zone; session killed | **Human — from either actor. Never the Editor.** |
| `infra-failed` | Setup failure, wall-clock timeout (exit 124), rate limit, OOM | **Human — from either actor. Never the Editor.** |

`integration-failed` is the one outcome that does not classify a *session*: the Implementer
session already succeeded green in isolation. The merge queue raises it when that green tree
will not integrate with a sibling that landed first — precisely the kind of failure the
Implementer cannot observe about itself, so it goes to the Editor rather than back to the
actor that produced it.

**There is no retry anywhere in this system.**

`infra-failed` pages the human immediately, from either actor, and never reaches the Editor. A
stale lockfile, a 429, an OOM, a wall-clock kill: none of these are fixed by running the same
session again against the same broken environment. Retrying would burn the budget, delay the
notification, and — because the failure is invisible to the model — produce a second failure
identical to the first. The honest move is to stop and say so.

Like `ceiling-exceeded`, an `infra-failed` session **spends no cycle**, because no cycle
occurred: a cycle is an Implementer session plus the Editor session that follows it, and no
Editor is involved. The sub-issue goes to `needs-human` with its worktree preserved, and
quarantine-and-drain does the rest — the run continues, and everything not downstream of it
still lands.

So the two outcomes the Editor never sees — `ceiling-exceeded` and `infra-failed` — share one
destination and differ only in what they tell the human: *the sub-issue was cut too large*
versus *your environment is broken*. Same page, different morning; one sends you to the graph,
the other to the lockfile. That is why they remain distinct outcomes despite the shared route.
**The notification carries the diagnosis, and the diagnosis is the product.**

**The model's word for its own outcome; the harness's word for everything the model cannot
observe about itself.** Ralph already trusts a sentinel this way — `<promise>NO MORE
TASKS</promise>`. Extend the pattern.

### Why this is not optional

Dependency installation fails. The agent starts in a worktree with no `node_modules`.
Every test fails with `Cannot find module`. It writes a test — fails. Writes an
implementation — fails. Tries a different approach — fails identically. Behaving exactly
as designed, it emits `<impasse>`: *"I cannot make these tests pass."*

An Opus Editor now spins up, reads the worktree, and is asked whether the **specification**
is wrong.

That is a full Editor session, at Opus prices, diagnosing `npm ci`. It can happen three
times before the human is paged, with the Editor rewriting a perfectly good brief each
cycle. The notification finally reads *"inconclusive after three cycles"* — the most
alarming message the system can send — and it means the lockfile was stale.

A 429 across N parallel agents on one API key produces the same signature. So does an OOM
kill. So does the 120k ceiling: a session killed for leaving the smart zone exits non-zero
and, unless marked `ceiling-exceeded`, is indistinguishable from a crash — and gets retried,
straight back out of the smart zone.

Pre-commit hooks do not help here. They *cause* this: the hook rejects the commit, the
agent thrashes, the agent declares an impasse. **Hooks protect the branch. Classification
protects the budget.** They are orthogonal.

### Rules

- **Zero commits is never a benign skip.** A session that produced nothing is an `impasse` (the
  model gave up, whether or not it said so) or `infra-failed`. Today's `no commits - skipping`
  silently treats it as success.
- **The suite result, not the exit code, is the outcome.** The harness runs the tests. The
  prompt *asks* the agent not to commit on red; nothing verifies that.
- Wrap `codex exec` in `timeout`; treat exit 124 as `infra-failed`.
- A non-zero exit **without** the sentinel means the process died — do not assume the model
  gave up.

---

## 6. State, trust, and reconciliation

**Linear is the source of truth for the plan.** The Planner owns it.
**Git is the source of truth for the work.** The Implementer owns it.
Neither is ever consulted about the other's domain.

Write-through, read-once. The run writes state transitions, impasse reports, revisions, and
verdicts to Linear as they happen — best-effort telemetry. A failed Linear write is logged
and retried at run end; it never fails a run. Nothing is ever read back from Linear during
a run.

Editor revisions land as **comments plus a distinguished revised-brief field**, never as
an overwrite of the Planner's original description. The original intent is the only thing a
bad outcome can later be diffed against.

### The run log

One append-only file per run — the local record the earlier phases rely on, since Linear
sync is the last piece to land. Each line is one event: a timestamp, the sub-issue it concerns, and
what happened. It tracks only two kinds of thing:

- **States.** A session started; a session finished with its outcome (`success`, `impasse`,
  `ceiling-exceeded`, `infra-failed`); a sub-issue reached a terminal state
  (`landed`, `needs-human`, or skipped).
- **Decisions.** The verdict an Editor returned (`revise`, `planning-defect`,
  `inconclusive`).

Nothing else. Token spend, diffstats, and failing-test output belong in the impasse report,
not here. The run log answers one question — *what happened, in what order, to which
sub-issue* — and it is exactly what the write-through mirrors to Linear, best-effort.

### The merge/write window

The merge queue's last two steps — fast-forward, then write `landed` to Linear — are not
atomic. **Merge first, then write.**

This fails toward redundant work (a `landed` sub-issue looks pending; the next run
redispatches it, an Implementer finds the tests already green, one session wasted) rather
than toward missing code (a pending sub-issue looks `landed`; dependents build against a
baseline that never received it, and an Editor is summoned to diagnose a brief that is
fine).

Note the ordering protects against **the harness dying**, not against Linear being down. If
Linear is down, both orders fail identically and the write is simply retried.

Accepted: the window is one API call wide, the lock is held across it, and runs are
supervised.

### CI is the trust boundary

Every check in this system runs **inside the agent's blast radius**: pre-commit hooks, the
merge queue's suite, the Editor's reproduction. The agent has had `workspace-write` for the
whole session with `--ask-for-approval never`. An agent that adds a `conftest.py` fixture
stubbing a dependency, edits `vitest.config.ts` to exclude a directory, hand-installs a
package, or writes a `.env` the suite reads, has produced a suite that is green **only
there**.

No malice is implied. Every green result is produced inside the blast radius of the thing
being tested.

**CI on the PR is the only check that runs on a clean checkout the agent never touched.**
`ralph validate` must refuse a repo without PR CI. CI is the natural home for a fresh install
from the lockfile, a diff of test-config files, and a suite run with none of the agent's
leftover artifacts.

---

## 7. Multi-repo

**One repo, one Kanban, one parent issue, one PR.** The Planner is scoped to a single
repository. There are no cross-repo dependency edges, because a git worktree does not span
repositories.

Linear is therefore chosen for its **Kanban, its API, and its independence from any one
GitHub org** — an observability and reuse argument, not a dependency-management one. The
sync layer stays thin, write-through, and out of the critical path.

Cross-repo sequencing is a different system and is out of scope.

---

## 8. Bets accepted deliberately

These are bets, not assumptions. They were argued and taken with the costs visible.

### 1. The Implementer's self-authored tests match the prose spec

The brief specifies tests in prose. The Implementer writes both the test and the code that
passes it. **No mechanical check on that correspondence exists.**

*"Invalid credentials return 401"* is satisfied, in letter, by a test that constructs a
response object and asserts its status code without ever calling the handler. Red against a
stub, green after any implementation, meaningless.

**If this bet loses, the failure is green.** The escalation ladder triggers on red and will
never fire. Mitigated by: small sub-issues, the `/tdd` skill, the integration sub-issue,
and human PR review.

### 2. The Editor does not soften specs into meaninglessness

The Editor rewrites the brief — including its tests — in response to an impasse report
authored by the actor that failed. It may do this up to twice more. **There is no budget on
softening and no record of drift.**

Each softening looks locally reasonable. *"The Implementer couldn't make concurrent writes
safe; I'll scope this to single-writer and file the concurrency work separately."* That is a
sentence a good senior engineer writes. Three times across three cycles, it is how a
sub-issue arrives green having implemented nothing that was asked for.

The system has a monotone: **difficulty only ever goes down.** No actor pushes it back up.
The Planner is never consulted. The Editor is measured on unblocking. The Implementer
benefits from softening — and since declaring an impasse is cheap, *the fastest route
from a hard sub-issue to a green one is to declare an impasse and let Opus make the
sub-issue easier.* The three-cycle cap bounds how many times this can happen, not whether
it does.

Accepted without mitigation. No `spec-drift` section on the PR.

### 3. The Editor's judgment on what knowledge survives

The Editor reads the failed worktree and learns things nobody else knows — that the
migration must precede the index, that the client's retry logic swallows the expected error,
that the hallucinated API is really called something else. That knowledge cost a full cycle.

The worktree is then deleted. The **findings** field is its home, but writing there is
still the Editor's choice: knowledge survives **only** if the Editor chooses to record it.
The field exists; using it well is unmandated.

### 4. Runs are supervised

No laptop sleep. No `Ctrl-C`. No branch switching mid-run. Runs execute in the working
checkout, and `trap cleanup EXIT` force-removes in-flight worktrees — including one an
Editor may be reading.

Cheap guard, taken regardless: assert `HEAD == ORIGINAL_BRANCH` before every merge and abort
loudly otherwise. Protection against a stray `git checkout` from another tool, not against
the operator.

### 5. Silent failures are acceptable

A parent issue can merge green having implemented a materially weaker feature than specified,
and nothing will say so — not the tests, not the ladder, not the notification.

**Bets 1, 2, 3 and 5 all resolve to the same place: the human reads the PR.** That is the
load-bearing human step. Everything else in this system is scaffolding to produce a PR worth
reading.

### A note on "small sub-issues bound the risk"

Small scope does bound the blast radius per Editor decision. But it moves risk rather than
removing it: more sub-issues means more edges, more parallel branches, more merge-queue
contention, more seams to mock across, and more Editor invocations in aggregate. Per-decision
risk falls; the number of decisions rises. Drift is redistributed from a few large swerves
into many small ones — harder to see in a PR, not easier.

And the risk lands on **decomposition**, the artifact with no automated feedback.

> **The grilling of the Planner is not a nice-to-have front-end. It is the primary quality
> mechanism of this system.**
