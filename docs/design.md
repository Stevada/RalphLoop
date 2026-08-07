# Ralph Loop — Design rationale

Harnessed engineering for coding models. Four actors, one medium, one repo, one PR.

This is the **why**: the design and the reasoning behind it, including the risks accepted
deliberately. The first implementation has been built against it — where the code and this document
disagree, the code is the fact and this is the intent it was meant to serve.

Vocabulary is canonical in [`UBIQUITOUS_LANGUAGE.md`](../UBIQUITOUS_LANGUAGE.md); this document uses
those terms and does not define them.

---

## 1. Actors

Four actors. They never talk to each other. Three of them talk to a sub-issue's **spec** and
**findings**; the fourth talks only to git.

| Actor | Backed by | Writes | Reads |
|---|---|---|---|
| **Planner** | Claude Opus, in conversation | The issue graph and the first draft of every spec | PRD, codebase, integration branch |
| **Editor** | Claude Code, Codex, **or** Copilot | A single sub-issue's spec and findings | Everything; may run read-only commands |
| **Implementer** | Codex **or** Copilot | All code, tests included | Its spec and findings, the repo |
| **Integrator** | Codex **or** Copilot | Anything in a conflicted worktree, plus the commit that resolves it | The conflict, both sides' history, the repo |

The Planner is invoked by a human, in conversation. The other three run unattended inside a run.

**An actor is a role, not a model.** Either Codex or Copilot can back any of the three unattended
roles; `cli.py` chooses from CLI arguments, and nothing downstream knows which is running. That is a
portfolio decision, not a hedge: the Implementer and the Editor should not be the same model on the
same failure, because an Editor adjudicating an impasse declared by *itself* is the least independent
sensor the system could have.

The Integrator is the one actor with no spec. It is not asked whether the work is right — that
question was already answered when the sub-issue reached the merge gate. It is asked only to make
two correct trees into one, which is why it is dispatched by the gate rather than the scheduler,
and why its failure pages a human instead of reaching an Editor.

### Transport per vendor

Ralph adopts SDK-backed transports per `(vendor, role)` where they earn their keep, not as a blanket
rewrite. The role cores need bounded text, token usage, auto-compaction telemetry, and — for the
Editor — a read-only guarantee. Copilot's SDK earns that place for both roles because it surfaces
usage and compaction as events and gives the Editor a permission request hook. Claude Code earns it
for the Editor because `can_use_tool` lets Ralph enforce the same allowlist before a tool runs.
Process transport remains for scripted stand-ins and any future command-only actor; removing it would
delete useful test coverage rather than simplify the harness.

### Codex decision

Codex uses the SDK session seam rather than the older subprocess Implementer adapter. The "SDK" is
Ralph's typed wrapper around `codex exec --json`: the child process is still Codex CLI, but the rest
of the harness sees a `TurnStreamSession` that emits text, completed-turn usage, and
auto-compaction telemetry. That keeps Codex on the same role cores as Copilot and Claude Code:
Implementer output flows through the shared SDK Implementer telemetry path, and Editor output flows
through `run_editor`.

The cancellation spike is a process-bound guarantee: `kill()` kills the running `codex exec` child,
the JSONL stream drains to EOF, and the stream reader ends rather than raising through the harness.
The wall-clock backstop therefore bounds both Codex roles through the same `run_turn_stream` path.

The read-only spike found no Codex per-tool pre-execution veto equivalent to Claude Code's
`can_use_tool` callback or Copilot's permission request hook. Codex's Editor guarantee is therefore
OS-enforced with `--sandbox read-only`, not callback-enforced through `read_only()`. That is the
same guarantee in kind — the Editor cannot write the worktree — with a different mechanism. The
cost is real: Codex CLI 0.143's read-only sandbox also denies temp/cache writes, so `uv run pytest`
does not run unchanged inside it. Ralph preserves the write guarantee rather than downgrading Codex
Editor to `workspace-write`; a repo that wants Codex as Editor must give Ralph a suite command that
is read-only under that sandbox, or the Editor session will fail visibly as infrastructure.

---

## 2. Invariants

These are load-bearing. Violating any of them collapses a boundary drawn elsewhere.

1. **A sub-issue's spec and findings are the only channel between actors.**
2. **Structure is immutable within a run.** The Planner's graph — sub-issues and their
   blocking edges — is fixed when the run reads it at start. The Editor may rewrite a spec
   and its findings; it may never add, remove, or re-link a sub-issue.
3. **A run reads the issue graph exactly once, at start.** No mid-run reads. Human
   intervention ends the run; resumption is a new run that re-reads the graph.
4. **The Editor writes only the spec and findings.** It may read anything and run read-only
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
                 │ sub-issue        │   (no tests — tests are prose in each spec)
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

The run reads the issue graph once, at start, and works from that read for its whole life.
Everything the Implementer and Editor read derives from it. Nothing reads the issue source again.

### 4.2 Command discovery and readiness

Before any actor starts, Ralph discovers the target repo's commands from the command descriptor.
The `test` command is required; `install` is optional. A repo with no discoverable `test` command is
refused by pre-flight rather than treated as a repo whose quality bar can be supplied at invocation
time.

That is the source-vs-verdict split. The command source belongs to the target repo because where a
repo is installed and tested is a property of that repo: it should be versioned, reviewed, and
changed with the code it validates. The verdict still belongs to the harness because Ralph must run
the discovered command itself at the point of integration and classify the result; moving command
ownership into the repo does not turn a model's self-report into evidence.

### 4.3 Dispatch

A sub-issue is **eligible** when every sub-issue it is blocked by has `landed`. Eligible
sub-issues are dispatched concurrently, each into its own git worktree branched from the current
integration head.

There is **no wave barrier.** Waves are an artifact of dependencies, not of merging. A
fast sub-issue lands in three minutes rather than waiting for its slowest sibling.

If the command descriptor declares `install`, dependencies are installed **once, in the base
checkout**, before any worktree exists. Installation failure is loud and fatal — never `|| true`.

### 4.4 The Implementer's session

One continuous Implementer session. It writes tests first, then implementation, per `/tdd`. It
iterates as it sees fit — running the suite, fixing, trying again — using its own judgment about
when it has reached an impasse.

That inner loop is the *model's*, inside one session. Ralph still has no retry destination and no
backoff. A merge conflict is not an exception to that: it is not the same session run again, it is
a different actor — the Integrator — answering a question about landing order that the Implementer
could not have seen from inside its own worktree. Do not read this paragraph as licence to add a
retry.

It exits exactly one of two ways:

- **Green**, with a commit, and it proceeds to the merge gate.
- **`<impasse>`**, with a structured impasse report.

"About three tries" is **guidance in the prompt**, not a harness-enforced counter. The
harness enforces the cycle cap outside the model.

#### The impasse report

The Editor's only sensor. It is a defendant's statement, so the harness corroborates it.

The Implementer supplies: the failing test and its assertion output verbatim; the
approaches tried and why each was abandoned; the specific acceptance criterion it believes
unsatisfiable; what would make it satisfiable.

The harness appends, independently of the model's narration: session output, diffstat, files
touched, wall-clock, and tokens consumed.

**The model's story, checked against the harness's facts.** Those two disagreeing is
itself a signal worth surfacing.

#### The hard session bound

The session bound is the **wall-clock timeout**, enforced in real time from outside the model.
A session that re-runs a failing suite for too long has exceeded the session bound, and the honest
outcome is `infra-failed`.

Token consumption is recorded as telemetry — it is what the session cost — but nothing is gated on
it.

### 4.5 The merge gate

This is the piece that makes parallelism honest.

When an Implementer reaches green in its own worktree:

1. **Acquire the merge lock.**
2. **Merge** the current integration head into the worktree. On conflict, **reconcile** (below).
3. **Run the suite in its own worktree.**
4. On green: **fast-forward merge**, then write `landed` to Linear.
5. **Release the lock.**

On a red suite on the prospective merge: **release the lock, preserve the worktree, and route the
sub-issue to the Editor** as an `integration-failed` outcome. The Implementer does not retry. A red
prospective merge is not a transient hiccup to paper over — it is a semantic conflict between two
trees that each satisfy their own spec, and adjudicating it is the Editor's job, not something the
actor that just failed to integrate should be trusted to fix by trying again.

The lock is held for merge, reconciliation, suite, fast-forward, release — never while the Editor
reasons. One sub-issue's *red* integration failure never stalls the gate for its siblings.

#### Reconciliation, when the merge conflicts

A textual conflict is a different animal, and it gets a different answer. It says nothing about
whether either spec was right; it is an artefact of the order two siblings happened to land in.
Sending it to an Editor would invite a spec rewrite for a problem no spec caused.

So the gate answers it itself, without releasing the lock:

1. **Dispatch the Integrator** into the conflicted worktree. It may change anything it needs to and
   it commits its resolution, exactly as the Implementer commits its work. Two commits result, in
   order: what the Implementer wrote, then what it took to make that land.
2. **Check that the merge is finished**, then continue at step 3 above. The suite gate is not
   waived — a conflict resolved perfectly can still leave a tree that does not build, and the
   Integrator's own opinion of its work is worth exactly what any actor's is.

Two exits, and the sub-issue never re-enters the gate: it lands, or it goes to the human. Holding
the lock across a model session costs throughput on the rare run where conflicts are frequent. It
buys correct-by-construction: nothing can move the integration head between the reconciliation and
the fast-forward that depends on it.

#### Why merge and not rebase

Step 2 merges rather than rebases, and that choice is what makes the rest of this section short.

A rebase re-creates the Implementer's commit: it detaches HEAD onto the integration head and replays
the work as a patch, moving the branch ref only when the replay finishes. A conflict therefore
strands the worktree in a state that is not a commit and not a branch — and an actor that resolves
it perfectly, but stops before finishing the replay, leaves a branch ref that never moved and a
commit count that reads as zero. Every mechanism the harness would need to survive that (finishing
the replay on the actor's behalf, measuring success against refs instead of HEAD, forbidding the
abort that would silently discard the resolution) exists only because of the rebase.

A merge has none of it. HEAD stays attached, the Implementer's commit stays where it is, and the
resolution is an ordinary commit made by an ordinary `git commit`. The Integrator needs no
git-specific contract at all: it edits a worktree and commits, which is what an Implementer does.

The price is that the integration branch is no longer linear — a merge commit appears whenever a
sibling landed between a worktree being cut and that worktree landing. That is the honest record of
what happened, and it is cheaper than the machinery linearity would cost.

Because the suite runs on the *prospective* merge result, `git merge` in the harness is
only ever a fast-forward of an already-verified tree. The integration branch is correct by
construction.

The suite gate inside the merge gate is the harness's only suite run. The Implementer still runs whatever checks it needs
inside its own session; that is its feedback loop and its completion signal. A second harness run
immediately after the session would only repeat the Implementer's isolated view. The merge-gate run
is different: it runs on the prospective merged tree, so it catches two sub-issues that
were each green alone but break when combined, which the Implementer cannot observe from its
isolated worktree. It also catches a false green before landing, because the Editor fires only on
failures and cannot adjudicate success the harness never challenged.

#### Why the actor, not bash

`git merge` does not fire the `pre-commit` hook. A clean merge creates its commit without
invoking it. So per-branch hooks are at best **branch-local checks** — a strictly weaker
claim than "the merged tree is green," and the gap between those two claims is exactly
where semantic conflicts live:

> Sub-issue 104 renames an export. Sub-issue 105 imports the old name. Different files.
> No textual conflict. Both branches green. Merged tree: red.

If bash performed the merge, the break would surface waves later — no worktree, no context,
no attribution. Under the merge gate the failure is caught the moment it happens, on the
prospective merge against the exact sibling that landed first, and the **worktree is
preserved** for the Editor. A live actor with the failing tree in front of it decides
whether 105's spec should adapt to 104's rename or whether the two were badly cut — a
decision bash could never make, and one the already-exited Implementer is no longer around
to make either.

#### A failed merge is an Editor trigger, except for mechanical conflict recovery

A red suite on the prospective merge routes the sub-issue to the Editor as `integration-failed` —
immediately, on the first failure, with no Implementer requeue. A textual merge conflict gets one
cheaper mechanical path first: Ralph leaves the conflict markers in place, resumes the same
Implementer session once, and retries the landing once. If that still does not land, the Editor
reads the preserved worktree and the sibling that landed first. If 105 can adapt to 104, it says
`revise` and the Implementer restarts clean; if *"105 cannot land alongside 104"* because the two
were badly cut, it has exactly the evidence for `planning-defect`.

That trip through the Editor spends one of the sub-issue's three **cycles** — an
`integration-failed` is counted exactly like an impasse, so a sub-issue that keeps failing
to integrate is escalated at the third.

The merge gate itself does no model work outside reconciliation: the merge and the suite run are
mechanical, so a
sub-issue spends no tokens to land work it has already finished. Only the Editor, on an
integration failure, costs anything.

#### The merge gate as an instrument

A parent issue whose sub-issues sail through the gate was decomposed well. One whose
sub-issues fail to integrate was not — and the merge gate turns that into `integration-failed`
routes and `planning-defect` verdicts from the Editor, rather than mysterious failures three
waves later.

Decomposition quality has no other automated check in this system. This is it.

### 4.6 The Editor's session

Triggered by an `impasse` — declared or not — or an `integration-failed` merge. Never by
`infra-failed`, which is not a failure the Editor can help with.

The Editor:

- reads the spec, the findings, the failure report (an Implementer impasse report, or — for
  `integration-failed` — the harness's record, since the Implementer authored none), and the
  **failed worktree**;
- **runs read-only commands** — re-runs the suite, greps, checks whether the API the
  Implementer complained about actually exists;
- rewrites the spec and records findings;
- returns a verdict.

Reproduction, not inference. The difference between *"this cannot be done as specified"*
and *"the Implementer gave up early"* is often a single grep. Since declaring an impasse
is cheap, the Editor's ability to check the Implementer's story against the repository is
the only thing standing between us and a system where declaring an impasse always works.

**On Editor entry, the Implementer's work is discarded.** It restarts clean against the
revised spec. No code survives. Knowledge survives only if the Editor writes it into the
findings — that is the Editor's judgment, unmandated.

Verdicts:

| Verdict | Effect |
|---|---|
| `revise` | Spec and findings rewritten; Implementer restarts clean. |
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

The harness owns **three** failure outcomes, not one. This is the single highest-value piece
of harness logic.

| Outcome | Detection | Routes to |
|---|---|---|
| `impasse` | Session did not deliver: the `<impasse>` sentinel, or no commits | **Editor** |
| `integration-failed` | The suite is red on the prospective merged tree | **Editor** |
| `integration-failed` | The prospective merge conflicts | **Integrator**, inside the gate |
| `infra-failed` | Setup failure, wall-clock timeout (exit 124), rate limit, OOM | **Human — from any actor. Never the Editor.** |

`integration-failed` is the one outcome that does not classify a *session*: the Implementer
committed work that reached the merge gate. The merge gate raises it when that tree will not
integrate with a sibling that landed first — precisely the kind of failure the Implementer cannot
observe about itself, so it goes to another actor rather than back to the one that produced it.

**Which actor depends on how it failed, and the split is not arbitrary.** A red prospective merge is
a semantic conflict: two trees that each satisfy their own spec and contradict each other in
meaning. Only a spec can resolve that, so it goes to the Editor and spends a cycle. A *textual*
conflict is not evidence that either spec was wrong — it is an artefact of landing order, and
sending it to an Editor would invite a spec rewrite for a problem no spec caused. It goes to the
Integrator, inside the merge lock, and spends no cycle.

**There is no retry destination in this system.** The Integrator is not one: it is a different actor
with a different job, not the same session run again.

`infra-failed` pages the human immediately, from any actor, and never reaches the Editor. A
stale lockfile, a 429, an OOM, a wall-clock kill: none of these are fixed by running the same
session again against the same broken environment. Retrying would burn the budget, delay the
notification, and — because the failure is invisible to the model — produce a second failure
identical to the first. The honest move is to stop and say so.

An `infra-failed` session **spends no cycle**, because no cycle occurred: a cycle is an
Implementer session plus the Editor session that follows it, and no Editor is involved. The
sub-issue goes to `needs-human` with its worktree preserved, and quarantine-and-drain does the
rest — the run continues, and everything not downstream of it still lands.
**The notification carries the diagnosis, and the diagnosis is the product.**

**The model's word for its own outcome; the harness's word for everything the model cannot
observe about itself.** Ralph already trusts a sentinel this way — `<promise>NO MORE
TASKS</promise>`. Extend the pattern.

### Why this is not optional

Dependency installation fails. The Implementer starts in a worktree with no `node_modules`.
Every test fails with `Cannot find module`. It writes a test — fails. Writes an
implementation — fails. Tries a different approach — fails identically. Behaving exactly
as designed, it emits `<impasse>`: *"I cannot make these tests pass."*

An Opus Editor now spins up, reads the worktree, and is asked whether the **specification**
is wrong.

That is a full Editor session, at Opus prices, diagnosing `npm ci`. It can happen three
times before the human is paged, with the Editor rewriting a perfectly good spec each
cycle. The notification finally reads *"inconclusive after three cycles"* — the most
alarming message the system can send — and it means the lockfile was stale.

A 429 across N parallel Implementers on one API key produces the same signature. So does an OOM
kill.

Pre-commit hooks do not help here. They *cause* this: the hook rejects the commit, the
the model thrashes, the model declares an impasse. **Hooks protect the branch. Classification
protects the budget.** They are orthogonal.

### Rules

- **Zero commits is never a benign skip.** A session that produced nothing is an `impasse` (the
  model gave up, whether or not it said so) or `infra-failed`. Today's `no commits - skipping`
  silently treats it as success.
- **A committed, impasse-free Implementer session reaches the merge gate.** The merge gate runs the
  suite on the prospective merge; that is where a false green becomes `integration-failed`.
- Apply the wall-clock bound to the actor session; treat a killed session as `infra-failed`.
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

Editor revisions land as **comments plus a distinguished revised-spec field**, never as
an overwrite of the Planner's original description. The original intent is the only thing a
bad outcome can later be diffed against.

### The run log

One append-only file per run — the local record the earlier phases rely on, since Linear
sync is the last piece to land. Each line is one event: a timestamp, the sub-issue it concerns, and
what happened. It tracks only two kinds of thing:

- **States.** A session started; a session finished with its outcome (`success`, `impasse`,
  `infra-failed`); a sub-issue reached a terminal state
  (`landed`, `needs-human`, or skipped).
- **Decisions.** The verdict an Editor returned (`revise`, `planning-defect`,
  `inconclusive`).

Nothing else. Token spend is deliberately excluded from the run log and persisted as
per-sub-issue consumption on the issue record; diffstats and failing-test output belong in the
impasse report, not here. The run log answers one question — *what happened, in what order, to
which sub-issue* — and it is exactly what the write-through mirrors to Linear, best-effort.

### The transcripts

What the run log leaves out has to go somewhere, or a run's only surviving account of a session is
the handful of facts the harness chose to classify. Each session's full output is kept in a file of
its own, at `<repo>/.scratch/<phase>/transcripts/<sub-issue>/<cycle>-<actor>.log`.

It is taken off the stream **before** the adapter reads anything out of it, and this is the whole
bet. Every adapter also parses that stream into turns — text, usage, compaction — and every parser
is only as current as the shapes it was taught. Assemble the transcript out of those turns and it
inverts the moment a vendor moves a key: what survives is what *failed* to parse, which is the
banners and the noise, while the model's own words go silently missing. So `SessionTelemetry`
carries two strings and not one. `session_output` is the reading, and the sentinels are parsed out
of it; `transcript` is the stream, and nothing is allowed between it and the disk. A parser may fall
behind a vendor. What a human is owed when a session goes wrong may not.

The two artifacts index each other without either referencing the other: a `session-finished` line
already names the sub-issue and the actor, and the cycle is the only thing a reader brings to the
filename. That is also why the key is three parts rather than one — a sub-issue can produce an
Implementer, an Editor and an Integrator session per cycle, three cycles over, and a coarser key
would quietly keep one of nine.

Nothing in the harness reads them back. They exist for the human who opens a quarantined worktree
and wants to know what the session was thinking — the one question the notification cannot answer
and the diff can only partly. A file that is empty is a session that said nothing, which is a
different claim from a file that is not there.

### The merge/write window

The merge gate's last two steps — fast-forward, then write `landed` to Linear — are not
atomic. **Merge first, then write.**

This fails toward redundant work (a `landed` sub-issue looks pending; the next run
redispatches it, an Implementer finds the tests already green, one session wasted) rather
than toward missing code (a pending sub-issue looks `landed`; dependents build against a
baseline that never received it, and an Editor is summoned to diagnose a spec that is
fine).

Note the ordering protects against **the harness dying**, not against Linear being down. If
Linear is down, both orders fail identically and the write is simply retried.

Accepted: the window is one API call wide, the lock is held across it, and runs are
supervised.

### CI is the trust boundary

Every check in this system runs **inside the actor's blast radius**: pre-commit hooks, the
merge gate's suite, the Editor's reproduction. The actor has had `workspace-write` for the
whole session with `--ask-for-approval never`. A model that adds a `conftest.py` fixture
stubbing a dependency, edits `vitest.config.ts` to exclude a directory, hand-installs a
package, or writes a `.env` the suite reads, has produced a suite that is green **only
there**.

No malice is implied. Every green result is produced inside the blast radius of the thing
being tested.

**CI on the PR is the only check that runs on a clean checkout the actor never touched.**
`ralph validate` must refuse a repo without PR CI. CI is the natural home for a fresh install
from the lockfile, a diff of test-config files, and a suite run with none of the actor's
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

The spec states tests in prose. The Implementer writes both the test and the code that
passes it. **No mechanical check on that correspondence exists.**

*"Invalid credentials return 401"* is satisfied, in letter, by a test that constructs a
response object and asserts its status code without ever calling the handler. Red against a
stub, green after any implementation, meaningless.

**If this bet loses, the failure is green.** The escalation ladder triggers on red and will
never fire. Mitigated by: small sub-issues, the `/tdd` skill, the integration sub-issue,
and human PR review.

### 2. The Editor does not soften specs into meaninglessness

The Editor rewrites the spec — including its tests — in response to an impasse report
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
removing it: more sub-issues means more edges, more parallel branches, more merge-gate
contention, more seams to mock across, and more Editor invocations in aggregate. Per-decision
risk falls; the number of decisions rises. Drift is redistributed from a few large swerves
into many small ones — harder to see in a PR, not easier.

And the risk lands on **decomposition**, the artifact with no automated feedback.

> **The grilling of the Planner is not a nice-to-have front-end. It is the primary quality
> mechanism of this system.**
