# Ralph Loop — Harness Architecture

A **map** of the harness: the layers, where each concept lives, the seams, and the invariants that
span files. It is written to be read in one sitting by someone about to change the code — enough to
know *which file to open* for any given task, and *why the boundaries are where they are*.

It is deliberately **not** a copy of the code. Signatures and function bodies live in `ralph/`,
where mypy keeps them honest; duplicating them here would be a second source of truth that drifts.
So each type and rule below is named, given a one-line job, and linked to its module — open the
module for the current signature.

- **Why** any of this is shaped the way it is: `docs/design.md`.
- **Terms** (canonical, including identifiers): [`UBIQUITOUS_LANGUAGE.md`](../UBIQUITOUS_LANGUAGE.md).
  An "aliases to avoid" word appearing as a class, function, field, or state name is a defect.
- **How each CLI exposes its context signal**: [`docs/cli-metering.md`](cli-metering.md).

## One process, asyncio

The load-bearing structural fact, because it deletes code. A **run** is a single process: the merge
lock is an `asyncio.Lock`, not `flock`. No PID tracking, no result files, no polling to reap finished
sessions. If you reach for any of those, you have mistranslated the design. (The bash prototype that
made this worth stating has been deleted.)

## Layers

The dependency arrow points inward, always.

```
ralph/
  domain/          pure. no I/O. stdlib only.
    __init__.py    ← THE INTERFACE. import `from ralph.domain import Outcome`, never from
                     `ralph.domain.model.session`. the layout below is nobody else's business.
    model/         the NOUNS — frozen values, zero logic. what the system is made of.
      session.py   Actor, Outcome, SessionTelemetry, SuiteResult
      impasse.py   Approach, ImpasseReport        ← the model's claim
      failure.py   FailureReport                  ← the claim + the harness's facts
      verdict.py   Verdict, EditorVerdict
      notification.py  Escalation, Notification   ← the one thing a human reads afterwards
    rules/         the VERBS — pure functions. what the system DECIDES.
      classify.py     session → Outcome            (the failure taxonomy)
      routing.py      (actor, outcome) → Destination   (and never a retry)
      eligibility.py  graph + states → what may run    (quarantine-and-drain)
      cycles.py       CycleLedger                      (the cap of three)
      report.py       → FailureReport      (and the impasse it will NOT invent)
      notify.py       → Notification       (what each failure cost, and what to open first)

  issues/          issue tracker values + seam + markdown storage.
    __init__.py    ← THE INTERFACE for issue values.
    graph.py       SubIssueId, SubIssue, IssueGraph, GraphError
    content.py     Brief, Findings
    state.py       SubIssueState
    store.py       IssueStore Protocol
    filesystem.py  FilesystemIssueStore, IssueParseError
  ports.py         Protocols — the seams. every one has a fake.
  runlog/          Event, EventKind, event(), JsonlRunLog — the authoritative run ledger
  adapters/        codex, copilot, claude_editor, context, prompt, session,
                   git, suite
  mergequeue.py    \  the merge queue and the scheduler, so not an adapter either
  scheduler.py      } orchestration — depends on ports only, never on a concrete adapter
  cli.py           composition root — the only place a concrete adapter is named

tests/
  fakes.py         a fake per Protocol. adapters: they satisfy an interface at a seam.
  builders.py      telemetry(), graph_of(), … values, not adapters. a different thing.
  testbed.py       a REAL git repo and a REAL stand-in agent subprocess. neither is a fake.
```

The rules that hold this shape together:

- **`rules/` is the harness.** Six files hold the entire design — the failure taxonomy, that nothing
  is retried, how quarantine drains, the cycle cap, the report the harness will not fabricate, and
  which failure to open first. Everything else exists to feed them. To know what the system
  *decides*, read one folder.
- **Issue tracker values live in `issues/`; eligibility stays in `domain/rules/`.** The graph,
  content, and state are what the tracker stores. The question "who may run?" is still a harness
  decision over those values.
- **`rules/` may import `model/`; `model/` may not import `rules/`** — a test enforces it. A value
  that knows how it will be classified has stopped being a value.
- **`domain/__init__.py` is the interface.** Import `from ralph.domain import Outcome`; the internal
  `model/` ⁄ `rules/` layout is not a caller's business.
- **The domain is not split by actor, and `adapters/` is not split by port.** `Outcome`,
  `SessionTelemetry`, and `SuiteResult` belong to *both* actors; `route(actor, outcome)` is about
  both; `copilot.py` is *both* an Implementer and an Editor. Either split would strand the shared
  types in a `shared/` folder that swallows most of the domain.
- **The fakes live under `tests/`, not in the package.** Ralph is an application: nothing downstream
  imports `ralph.fakes`, and a test asserts the package imports nothing from `tests/`. That is what
  makes "no adapter may reach for a fake" enforceable rather than aspirational.

The Implementer and the Editor are each chosen at startup — Codex or Copilot implements, Claude Code
or Copilot edits — and nothing downstream of `cli.py` knows which.

---

## 1. Issues — `ralph/issues/`

Issue tracker values and storage. The values are pure; the filesystem store is the markdown-backed
adapter used today.

### Structure vs. content

A **sub-issue** is immutable for the life of a **run**; its **brief** and **findings** are not (the
**Editor** rewrites them). So structure is a frozen value and content lives behind a store — which
is what makes "the Editor may never add, remove, or re-link a sub-issue" a property of the *types*
rather than a rule someone must remember.

| Type | Module | What it is |
|---|---|---|
| `SubIssueId`, `SubIssue`, `IssueGraph`, `GraphError` | [graph.py](../ralph/issues/graph.py) | The immutable graph. `IssueGraph` raises on cycles and dangling edges at construction. There is **no `kind`** field — contract/impl/integration roles are fully encoded in the `blocked by` edges, and a `kind` would be a second, un-checkable source of truth. |
| `Brief`, `Findings` | [content.py](../ralph/issues/content.py) | The two mutable fields. `Brief` = *what "done" means* (revision 0 is the Planner's, never overwritten); `Findings` = *what the last session learned*, difficulty-neutral, kept out of the brief so the brief stays clean as spec. |
| `SubIssueState` | [state.py](../ralph/issues/state.py) | `ready` → `in-progress` → `landed` \| `needs-human`. `landed` is a sub-issue's terminal state; `done` is the *parent's* and is banned here. |

`IssueStore` ([store.py](../ralph/issues/store.py)) is the tracker seam: files today, Linear later.
`content()` returns the **newest** revision. `write_event` is **best-effort** — a run must not die
because Linear was unreachable.

`FilesystemIssueStore` ([filesystem.py](../ralph/issues/filesystem.py)) reads
`.scratch/<phase>/issues/*.md` with numeric-prefix edges. `LinearIssueStore` will implement the same
Protocol and change nothing in the scheduler — which is how the Linear-sync gap collapses into
"write a second class."

---

## 2. Domain — `ralph/domain/`

Pure functions over frozen dataclasses. Tested with no subprocess, no git, no model.

### Session outcomes and the claim/corroboration split

| Type | Module | What it is |
|---|---|---|
| `Actor`, `Outcome`, `SessionTelemetry`, `SuiteResult` | [session.py](../ralph/domain/model/session.py) | The classification vocabulary. `SessionTelemetry` is *what the harness observed* — the ceiling reads `peak_context_tokens`; `consumed_tokens` is telemetry only, gated on nothing. `SuiteResult.green` is deliberately not `verified`: a suite the harness runs is inside the **blast radius**; only CI on a clean checkout is **honest** (`docs/design.md` §6). |
| `Approach`, `ImpasseReport` | [impasse.py](../ralph/domain/model/impasse.py) | The model's narration — a leaf that knows nothing about how it was classified. |
| `FailureReport` | [failure.py](../ralph/domain/model/failure.py) | The claim beside the harness's facts. It sits downstream of `session.py` (which imports `impasse.py`); splitting them is what breaks the import cycle. The Editor's job is to check one against the other — *their disagreeing is itself a signal*. |

`classify_implementer` / `classify_editor` ([classify.py](../ralph/domain/rules/classify.py)) turn
telemetry + suite into an `Outcome`. Two facts to know without reading the bodies: **precedence is
load-bearing** (a ceiling kill and a crash both exit non-zero and are only separated by checking
`killed` first), and **zero commits is never a benign skip** — it is an `impasse`. `INTEGRATION_FAILED`
is unreachable from either classifier; only the merge queue raises it.

### Routing — the taxonomy, executable

`route(actor, outcome) → Destination` ([routing.py](../ralph/domain/rules/routing.py)) is the
taxonomy table with one test per row. Four destinations — `MERGE_QUEUE`, `ACT_ON_VERDICT`, `EDITOR`,
`HUMAN` — and **no retry destination, because there is no retry anywhere in this system** (a test
asserts no row returns anything else).

Only `SUCCESS` needs to know who is asking (Implementer → merge queue, Editor → act on verdict).
`CEILING_EXCEEDED` and `INFRA_FAILED` route to the **human** from either actor, never to the Editor,
and spend no cycle; they keep separate identities despite the shared destination because they hand
the human different diagnoses (*cut too large* vs *environment broken*). The reasoning is
`docs/design.md` §5.

### Verdicts, the cycle cap, and lifecycle

| Type / function | Module | What it is |
|---|---|---|
| `Verdict`, `EditorVerdict` | [verdict.py](../ralph/domain/model/verdict.py) | `Verdict.is_terminal` is `True` for everything but `revise`. `EditorVerdict.revised_brief` is required iff `revise`. |
| `CycleLedger` | [cycles.py](../ralph/domain/rules/cycles.py) | The cap of three, hard-enforced. `must_be_terminal(id)` is `True` on the final cycle — the Editor may not return `revise`, and the **scheduler** refuses it rather than trusting the Editor to remember. One rule, one home. |
| `eligible`, `never_eligible` | [eligibility.py](../ralph/domain/rules/eligibility.py) | `eligible` = every blocker has `LANDED`, **derived never stored**. `never_eligible` is report-time only: a sub-issue still `ready` at run end whose blockers never landed **never got a turn** — distinct from `needs-human` ("I failed") without inventing a state for it. Nothing propagates a skip through the graph. |

---

## 3. Ports — `ralph/ports.py`

The seams. **Every Protocol has a fake, and the fakes are what the suite runs against** — a test that
needs a real model, network, or `codex` binary is in the wrong layer.

| Protocol | The seam | Notes that don't show in the signature |
|---|---|---|
| `Implementer` | writes code from a brief → `SessionTelemetry` | Either Codex or Copilot. |
| `Editor` | adjudicates a failure → `(SessionTelemetry, EditorVerdict \| None)` | Bounded exactly like an Implementer — same `Budget`, same telemetry, can come back `ceiling-exceeded`. Takes `must_be_terminal`; takes **no** `RunLog` or `IssueStore`, so every *consequence* of a verdict happens in the scheduler. |
| `RunLog` | the harness's **authoritative** record | A Protocol, not the JSONL adapter, because the merge queue and scheduler both take one and orchestration may not name an adapter. |
| `TestRunner` | a suite run → `SuiteResult` | The harness runs the tests; the model's exit code is only its opinion. |
| `Git` | worktree / rebase / ff plumbing | `discard_worktree` destroys the checkout **and the branch** — the next cycle re-cuts `ralph/<id>` from integration, and `git worktree add -b` refuses an existing branch. |
| `ContextSource` | one `Observation` per model call, live | An `AsyncGenerator`, not merely an iterator: **closing is part of the contract** (a source tailing a file holds a handle open, and the ceiling kill breaks the loop mid-stream). See §3. |

`Budget` ([ports.py](../ralph/ports.py)) carries the two bounds that catch different failures:
`max_context_tokens` (120k — the smart zone, a *quality* bound, one number for both actors) and
`wall_clock_s` (the backstop that catches a *stuck* session, whose context stays flat while it spins).
Neither substitutes for the other.

**Revisions are stored alongside the Planner's original, never over it.** The revision number is the
*store's* to assign — an Editor choosing its own could overwrite an earlier one, and only the store
knows what is on disk. Revision 0 is what a human diffs against to see whether three cycles of Editor
rewriting quietly softened the spec (`docs/design.md` §8, the bet most likely to fail). Layout:

```
.scratch/<phase>/issues/
  01-sub.md                     ← the Planner's, live. Its Status: line is mirrored into it.
  revisions/01/
    0-brief.md  0-findings.md   ← snapshotted on the FIRST revision, never rewritten
    1-brief.md  1-findings.md   ← the Editor's
    2-brief.md  2-findings.md
```

Brief and findings are separate files because a revision may change one and leave the other alone.

---

## 4. Adapters — `ralph/adapters/`

Two CLIs, and **either can back either actor**. Chosen in `cli.py` from `RALPH_IMPLEMENTER` /
`RALPH_EDITOR`; nothing else knows which is running.

|  | Implementer | Editor |
|---|---|---|
| **Codex** (`codex exec`) | ✅ | — |
| **Copilot** (`copilot -p`) | ✅ | ✅ |
| **Claude Code** (`claude-agent-sdk`) | — | ✅ |

A portfolio decision, not a hedge: the Implementer and Editor should not be the same model on the
same failure — an Editor adjudicating an impasse it declared *itself* is the least independent sensor
the system could have.

### The ceiling seam

The smart-zone kill is identical for every adapter; only the *publishing* of the context signal
differs per CLI. So the kill logic lives once, behind `ContextSource`:

- `ContextMeter` ([context.py](../ralph/adapters/context.py)) — watches context (not consumption),
  records the high-water mark, trips when the smart zone is left. `exceeded` reads the **peak**, not
  the last observation: a model that touched 130k then compacted to 90k has already done its bad
  thinking, and compaction must not hide the crossing.
- `run_bounded(proc, source, budget)` — the kill loop every adapter's `run`/`adjudicate` reduces to.
  Consumes the source under `aclosing`, kills on `exceeded` or `TimeoutError`. Takes a `Killable`
  rather than a subprocess, because the SDK Editor is a conversation, not a process.
- `source=None` is **not** "unbounded" — it is *a session publishing no context signal* (the stand-in
  agent, or a bare `RALPH_AGENT_CMD`): bounded on the clock alone, peak honestly reported as zero.

**Per-CLI empirics — the rollout-file format, the debug-log gotchas, the 56.5k→8.4k measurements —
live in [`docs/cli-metering.md`](cli-metering.md).** They are external to our code and change on the
vendors' schedule, not ours. What matters at *this* layer: the three numbers (`context_tokens`,
`consumed_tokens`, `rate_limit`) travel together in one `Observation` because that is how they
arrive — adjacent, alike-named — and a ceiling on the wrong one is *inverted*.

### An Implementer is an argv and a context source

That is the whole of `SubprocessImplementer` ([session.py](../ralph/adapters/session.py)), and it is
why `codex.py` is a hundred lines. Everything that makes a session a session — both bounds, counting
commits, reading the diffstat, finding the `<impasse>` sentinel — lives once in `session.py`; each
CLI adapter is an argv plus a `ContextSource`. [prompt.py](../ralph/adapters/prompt.py) is the one
place a `Brief` becomes text a model reads, shared by Codex and Copilot so their failures stay
comparable; findings go in as a **separate section**, never folded into the brief.

### The Editor writes nothing but brief and findings — enforced, not asked

The Editor may read anything and run read-only commands; the moment it commits it is an Implementer
with a different name. **This is enforced by a tool allowlist, not by the prompt.** What counts as
mutating is decided **once**, in [editor.py](../ralph/adapters/editor.py) (the model-agnostic half,
which also owns the `<verdict>` sentinel and the bounding). `ClaudeCodeEditor` is that plus the Agent
SDK's `can_use_tool` callback; `CopilotEditor` is that plus the CLI's `--available-tools` /
`--deny-tool` flags. Only the *delivery* of the denial differs.

Three things are load-bearing, each a hole in the obvious implementation:

- **It is an allowlist, not a blocklist.** A blocklist fails open on the tool nobody thought of — the
  one the SDK gains next week. `Write` is denied by *absence*.
- **Redirection and substitution are denied outright.** `git log > evidence.txt` passes any "is this
  read-only?" check — the write is in the shell, not the program — and it destroys the failed worktree
  a human was about to read. (For Copilot, *not* passing `--allow-all-tools` is what closes this hole.)
- **Every command in a pipeline is checked, not just the head.** `cat x | tee copy.py` begins harmless
  and ends as an Implementer.

The one thing the Editor may run that *executes* is the repo's own suite — the same command the
harness runs, passed in rather than guessed. `must_be_terminal` is surfaced **in the prompt** and
enforced **in the scheduler**; an Editor that returns no verdict classifies `infra-failed` and the
adapter returns `None` rather than inventing an `inconclusive` the parser fumbled into existence.

**`CopilotEditor`'s read-only guarantee is weaker than `ClaudeCodeEditor`'s** — not because the list
is shorter, but because `read_only()` is a pure function the harness owns and the suite attacks fifty
ways, while Copilot's enforcement lives inside a binary we cannot inspect or test. The tests pin that
the harness *asks* correctly; that Copilot *honours* the ask is a reasonable assumption, still an
assumption. Prefer the SDK Editor where the choice is free; Copilot exists so the Editor need not be
the same model as the Implementer, which matters more.

## 5. Orchestration

Depends on ports only, never on a concrete adapter.

### `MergeQueue` — [mergequeue.py](../ralph/mergequeue.py)

Sub-issues run in parallel but **land one at a time**. `land(wt)` holds the merge lock (an
`asyncio.Lock`) for exactly: check the integration head has not moved → rebase → **re-run the suite in
the worktree** → fast-forward. It returns a `Land(result, suite)`.

- The suite runs on the *prospective* merge result, so `merge --ff-only` is only ever a fast-forward
  of an already-verified tree: **the integration branch is correct by construction.**
- The lock is never held while the Editor reasons, so one sub-issue's integration failure never
  stalls the queue for its siblings.
- **The queue writes nothing, anywhere.** Deciding a tree may become the integration branch is its
  job; recording *that* a sub-issue landed is a state transition, and those belong to the scheduler —
  two writers for one fact is one too many.
- `Land` carries the suite out because that prospective-merge suite is the **only honest one** for an
  `integration-failed` sub-issue: the worktree's own run was green (that is why it reached the queue),
  and handing the Editor a green `SuiteResult` beside an integration failure would be a manufactured
  contradiction.

### `Scheduler` — [scheduler.py](../ralph/scheduler.py)

One dispatch loop: refuse a red base, read the graph once, then repeatedly dispatch every `eligible`
sub-issue and `await asyncio.wait(..., FIRST_COMPLETED)`.

- **`FIRST_COMPLETED`, never `gather`:** eligibility is re-derived on every completion, so a sub-issue
  starts the moment its blockers land. **There is no wave barrier.**
- A sub-issue is **claimed** (`states[id] = IN_PROGRESS`) *before* its coroutine can yield, in the
  same breath as the dispatch — the state map is the only thing standing between it and being
  dispatched twice.
- `_pipeline` holds a semaphore for a sub-issue's **whole** life, landing included: it is not finished
  until it is on integration, and counting it free while it waits for the merge lock would let the cap
  be exceeded where it matters. `_cycle` returning `None` means `revise` — go round again, clean.
- A cycle is **spent when the Editor half begins**, not when it ends: `must_be_terminal` must already
  count this cycle, or a killed Editor would cost nothing and buy its sub-issue infinite Implementers.
- **The `editor` is `Editor | None`, and `None` means there is no Editor in this run** — not a null
  one. An adapter that always returned no verdict would be a lie the taxonomy propagates faithfully
  (`classify_editor` calls a verdictless Editor `infra-failed`). Without an Editor the run is
  quarantine-and-drain; that is the cheap run, not a degraded one.

When a sub-issue escalates the run does **not** stop: everything transitively blocked by it never
becomes eligible, every unaffected sub-issue lands, and the run ends with **one** notification
(`docs/design.md` §4.7 for why quarantine-and-drain rather than fail-fast).

### `RunLog` and the two sinks — [runlog/](../ralph/runlog/)

Append-only, one line per event, two kinds of thing only: session states and Editor verdicts. Token
spend, diffstats, and failing-test output belong in the impasse report, not here.

`Event` and `EventKind` live in [runlog/model.py](../ralph/runlog/model.py), outside `domain/`,
because they are the ledger vocabulary rather than a domain decision. `event()` lives beside them
because it reads the clock, and the domain stays pure. `JsonlRunLog` is the concrete append-only
storage implementation in [runlog/jsonl.py](../ralph/runlog/jsonl.py). `ports.py` may import the
run-log value, but orchestration still depends on the `RunLog` Protocol rather than the JSONL
implementation. `Event` carries `actor` because a cycle closes **two** sessions against one
sub-issue — without it, `session-finished: infra-failed` could not say whether the Implementer or the
Editor crashed.

The run log (**authoritative** — failing to write it fails the run) and `IssueStore.write_event`
(**best-effort** — mirrors the transition into the tracker) are the same `Event` to two sinks of
different durability. A cycle's worth reads as a story with two characters:

```
01  implementer  session-started    in-progress
01  implementer  session-finished   impasse
01  editor       session-started    in-progress
01  editor       session-finished   success     ← the EDITOR's session succeeded…
01  editor       verdict-recorded   revise      ← …and this is what it found
01  implementer  session-started    in-progress ← cycle two, against a rewritten brief
01  implementer  session-finished   success
01  implementer  sub-issue-closed   landed
```

### The pre-flight — [preflight.py](../ralph/domain/rules/preflight.py), gathered in `cli.py`

**It refuses; it does not warn.** Five checks, each describing a repo the harness would otherwise
damage or misjudge:

| Check | What it would otherwise do |
|---|---|
| `protected-branch` | Fast-forward `main`. Ralph lands onto the branch it is run from. |
| `dirty-tree` | Fight the merge queue's fast-forwards over uncommitted work, and lose. |
| `no-suite` | Call every session green — an undeclared `impasse` becomes **unreachable**, the most expensive miss of the five. |
| `hooks-not-installed` | Land commits that skipped the checks the repo believes it enforces. |
| `graph` | Read a graph it cannot read. |

The **rule is pure** (`RepoFacts` in, `Refusal`s out), so each refusal's sentence is tested without a
repo to be wrong about; only the gathering is `cli.py`'s. It does not stop at the first refusal, and
it does not *paraphrase* — the graph's refusal quotes `IssueParseError` verbatim, because a cycle and
a missing acceptance criterion are different mornings. **`ralph run` runs the same checks and raises**
— a check that fires only when a human remembers to ask is a check the run does not have.
`ralph run --dry-run` reports the build order by asking **`eligible`** the same question the scheduler
asks, never by a second topological sort that is free to disagree.

---

## 5. Where each component lives

| Component | Module |
|---|---|
| Merge queue | [mergequeue.py](../ralph/mergequeue.py) |
| Failure taxonomy + base-green | [classify.py](../ralph/domain/rules/classify.py), [routing.py](../ralph/domain/rules/routing.py), [runlog/](../ralph/runlog/), `Scheduler._refuse_a_red_base` |
| Context ceiling and timeout | `Budget`, [context.py](../ralph/adapters/context.py) + per-CLI `ContextSource` ([cli-metering.md](cli-metering.md)) |
| Impasse report format | [impasse.py](../ralph/domain/model/impasse.py), [failure.py](../ralph/domain/model/failure.py) |
| The Editor | [claude_editor.py](../ralph/adapters/claude_editor.py), [copilot.py](../ralph/adapters/copilot.py), `CycleLedger` |
| Linear sync | `issues/linear.py` behind the existing `IssueStore` Protocol |
| Pre-flight + notification | [preflight.py](../ralph/domain/rules/preflight.py), `RunReport`, `cli.render` |
