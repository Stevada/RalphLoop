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
- **How each CLI exposes token usage telemetry**: [`docs/cli-metering.md`](cli-metering.md).

## One process, asyncio

The load-bearing structural fact, because it deletes code. A **run** is a single process: the merge
lock is an `asyncio.Lock`, not `flock`. No PID tracking, no result files, no polling to reap finished
sessions. If you reach for any of those, you have mistranslated the design. (The bash prototype that
made this worth stating has been deleted.)

## Layers

The dependency arrow points inward, always.

```
ralph/
  harness/         pure decision core. no I/O. stdlib only.
    __init__.py    ← THE INTERFACE. import `from ralph.harness import Outcome`, never from
                     `ralph.harness.model.session`. the layout below is nobody else's business.
    model/         the NOUNS — frozen values, zero logic. what the system is made of.
      session.py   Actor, Outcome, SessionTelemetry, SuiteResult
      impasse.py   Approach, ImpasseReport        ← the model's claim
      failure.py   FailureReport                  ← the claim + the harness's facts
      verdict.py   Verdict, EditorVerdict
    rules/         the VERBS — pure functions. what the system DECIDES.
      classify.py     session → Outcome            (the failure taxonomy)
      routing.py      (actor, outcome) → Destination   (and never a retry destination)
      eligibility.py  graph + states → what may run    (quarantine-and-drain)
      cycles.py       CycleLedger                      (the cap of three)
      report.py       → FailureReport      (and the impasse it will NOT invent)

  issues/          issue tracker values + seam + storage adapters.
    __init__.py    ← THE INTERFACE for issue values.
    graph.py       SubIssueId, SubIssue, IssueGraph, GraphError
    content.py     Spec, Findings
    state.py       SubIssueState
    store.py       IssueStore Protocol
    filesystem/    FilesystemIssueStore, IssueParseError
    linear/        LinearIssueStore, LinearGraphQLClient
  notification/    Escalation, Notification, notify() — the one human-facing run artifact
  ports.py         Protocols — the seams. every one has a fake.
  runlog/          Event, EventKind, event(), JsonlRunLog — the authoritative run ledger
  adapters/        claude, codex, copilot, commands, context, prompt, session, turn_stream,
                   git, suite
  mergegate.py     \  the merge gate and the scheduler, so not an adapter either
  scheduler.py      } orchestration — depends on ports only, never on a concrete adapter
  cli.py           composition root — the only place a concrete adapter is named

tests/
  fakes.py         a fake per Protocol. adapters: they satisfy an interface at a seam.
  builders.py      telemetry(), graph_of(), … values, not adapters. a different thing.
  testbed.py       a REAL git repo and a REAL stand-in subprocess. neither is a fake.
```

The rules that hold this shape together:

- **`rules/` is the harness.** Five files hold the core decisions — the failure taxonomy, that
  nothing is retried, how quarantine drains, the cycle cap, and the report the harness will not
  fabricate. Everything else exists to feed them. To know how the system *decides*, read one folder.
- **Issue tracker values live in `issues/`; eligibility stays in `harness/rules/`.** The graph,
  content, and state are what the tracker stores. The question "who may run?" is still a harness
  decision over those values.
- **Human notification lives in `notification/`.** It is pure, but it is no longer part of the
  harness interface: it assembles the end-of-run artifact from harness failures and issue graph cost.
- **`rules/` may import `model/`; `model/` may not import `rules/`** — a test enforces it. A value
  that knows how it will be classified has stopped being a value.
- **`harness/__init__.py` is the interface.** Import `from ralph.harness import Outcome`; the internal
  `model/` ⁄ `rules/` layout is not a caller's business.
- **The harness core is not split by actor.** `Outcome`, `SessionTelemetry`, and `SuiteResult` belong
  to *both* actors; `route(actor, outcome)` is about both. Adapter packages are layout, not harness
  policy; the shipped transports are listed below and the rationale lives in `docs/design.md`.
- **The fakes live under `tests/`, not in the package.** Ralph is an application: nothing downstream
  imports `ralph.fakes`, and a test asserts the package imports nothing from `tests/`. That is what
  makes "no adapter may reach for a fake" enforceable rather than aspirational.

The three unattended actors are each chosen at startup — Codex or Copilot implements, Codex or
Copilot reconciles, Claude Code / Codex / Copilot edits — and nothing downstream of `cli.py` knows
which.

---

## 1. Issues — `ralph/issues/`

Issue tracker values and storage. The values are pure; the filesystem store is the markdown-backed
adapter used today.

### Structure vs. content

A **sub-issue** is immutable for the life of a **run**; its **spec** and **findings** are not (the
**Editor** rewrites them). So structure is a frozen value and content lives behind a store — which
is what makes "the Editor may never add, remove, or re-link a sub-issue" a property of the *types*
rather than a rule someone must remember.

| Type | Module | What it is |
|---|---|---|
| `SubIssueId`, `SubIssue`, `IssueGraph`, `GraphError` | [graph.py](../ralph/issues/graph.py) | The immutable graph. `IssueGraph` raises on cycles and dangling edges at construction. There is **no `kind`** field — contract/impl/integration roles are fully encoded in the `blocked by` edges, and a `kind` would be a second, un-checkable source of truth. |
| `Spec`, `Findings` | [content.py](../ralph/issues/content.py) | The two mutable fields. `Spec` = *what "done" means* (revision 0 is the Planner's, never overwritten); `Findings` = *what the last session learned*, difficulty-neutral, kept out of the spec so the spec stays a clean bar. |
| `SubIssueState` | [state.py](../ralph/issues/state.py) | `ready` → `in-progress` → `landed` \| `needs-human`. `landed` is a sub-issue's terminal state; `done` is the *parent's* and is banned here. |

`IssueStore` ([store.py](../ralph/issues/store.py)) is the tracker seam: filesystem markdown or
Linear.
`content()` returns the **newest** revision. `write_event` is **best-effort** — a run must not die
because Linear was unreachable. `record_consumption()` stores each actor session's token
consumption per sub-issue; filesystem storage is the readable local record, and Linear mirrors the
same record as best-effort sub-issue comments.

`FilesystemIssueStore` ([filesystem/](../ralph/issues/filesystem/)) reads
`.scratch/<phase>/issues/*.md` with numeric-prefix edges. `LinearIssueStore`
([linear/](../ralph/issues/linear/)) reads a Linear parent issue and turns its native
sub-issues and `blocked by` relations into the same `IssueGraph`. The scheduler does not know which
store it is using.

In Linear mode, the current spec and findings live in the sub-issue description. Editor revisions
are append-only Ralph comments on that sub-issue: revision 0 snapshots the Planner's original, and
each later revision records the spec/findings Ralph just wrote back into the description.

---

## 2. Harness — `ralph/harness/`

Pure functions over frozen dataclasses. Tested with no subprocess, no git, no model.

### Session outcomes and the claim/corroboration split

| Type | Module | What it is |
|---|---|---|
| `Actor`, `Outcome`, `SessionTelemetry`, `SuiteResult` | [session.py](../ralph/harness/model/session.py) | The classification vocabulary. `SessionTelemetry` is *what the harness observed* — `consumption` is telemetry only, gated on nothing. `SuiteResult.green` is deliberately not `verified`: a suite the harness runs is inside the **blast radius**; only CI on a clean checkout is **honest** (`docs/design.md` §6). |
| `TokenConsumption` | [consumption.py](../ralph/harness/model/consumption.py) | The three disjoint token buckets and the total they sum to, one shape all three adapters translate into. A bucket is `None` where the vendor reported only a total, and adding an unknown bucket to a known one yields unknown — a sum missing one session's share would look exactly like a real measurement. |
| `Approach`, `ImpasseReport` | [impasse.py](../ralph/harness/model/impasse.py) | The model's narration — a leaf that knows nothing about how it was classified. |
| `FailureReport` | [failure.py](../ralph/harness/model/failure.py) | The claim beside the harness's facts. It sits downstream of `session.py` (which imports `impasse.py`); splitting them is what breaks the import cycle. The Editor's job is to check one against the other — *their disagreeing is itself a signal*. |

`classify_implementer` / `classify_editor` / `classify_integrator`
([classify.py](../ralph/harness/rules/classify.py)) turn telemetry into an `Outcome`. Three facts to
know without reading the bodies: **zero commits is never a benign skip** — it is an `impasse`;
`INTEGRATION_FAILED` is unreachable from the Implementer's and Editor's classifiers — only the merge
gate raises it; and the Integrator's classifier asks a question about git state rather than about
commit count, because a session that resolved a conflict and did not commit leaves a commit count
that reads as success.

### Routing — the taxonomy, executable

`route(actor, outcome) → Destination` ([routing.py](../ralph/harness/rules/routing.py)) is the
taxonomy table with one test per row. Four destinations — `MERGE_GATE`, `ACT_ON_VERDICT`, `EDITOR`,
`HUMAN` — and **no retry destination** (a test asserts no row returns anything else).

Two outcomes need to know who is asking. `SUCCESS`, because an Implementer's goes to the merge
gate, an Editor's is a verdict to act on, and an Integrator's continues the landing it is already
inside. `INTEGRATION_FAILED`, because the same word means different things by actor: from an
Implementer it says two trees disagree and an Editor should look; from the Integrator it says the
actor *sent* to reconcile them could not, and no spec was ever wrong. `INFRA_FAILED` routes to the
**human** from any actor, never to the Editor, and spends no cycle.

A merge conflict never reaches `route` at all. The merge gate raises it, dispatches the Integrator, and runs its suite gate on the result — all
inside one hold of the merge lock ([mergegate.py](../ralph/mergegate.py); `docs/design.md` §4.5 for
why). The sub-issue lands or goes to the human; it never re-enters the gate, so there is no
destination for it to route to.

### Verdicts, the cycle cap, and lifecycle

| Type / function | Module | What it is |
|---|---|---|
| `Verdict`, `EditorVerdict` | [verdict.py](../ralph/harness/model/verdict.py) | `Verdict.is_terminal` is `True` for everything but `revise`. `EditorVerdict.revised_spec` is required iff `revise`. |
| `CycleLedger` | [cycles.py](../ralph/harness/rules/cycles.py) | The cap of three, hard-enforced. `must_be_terminal(id)` is `True` on the final cycle — the Editor may not return `revise`, and the **scheduler** refuses it rather than trusting the Editor to remember. One rule, one home. |
| `eligible`, `never_eligible` | [eligibility.py](../ralph/harness/rules/eligibility.py) | `eligible` = every blocker has `LANDED`, **derived never stored**. `never_eligible` is report-time only: a sub-issue still `ready` at run end whose blockers never landed **never got a turn** — distinct from `needs-human` ("I failed") without inventing a state for it. Nothing propagates a skip through the graph. |

---

## 3. Ports — `ralph/ports.py`

The seams. **Every Protocol has a fake, and the fakes are what the suite runs against** — a test that
needs a real model, network, or `codex` binary is in the wrong layer.

| Protocol | The seam | Notes that don't show in the signature |
|---|---|---|
| `Implementer` | writes code from a spec → `SessionTelemetry` | Either Codex or Copilot. |
| `Integrator` | reconciles a conflicted worktree → `SessionTelemetry` | Takes only a `SessionContext`: no failure report, no `must_be_terminal`, no resumable identifier. The conflict is fully described by the repository it stands in, which is what lets *any* adapter play the role — a resumed-session contract would have restricted it to transports that can resume. Dispatched by the merge gate, never by the scheduler. |
| `Editor` | adjudicates a failure → `(SessionTelemetry, EditorVerdict \| None)` | Bounded exactly like an Implementer — same `Budget`, same telemetry. Takes `must_be_terminal`; takes **no** `RunLog` or `IssueStore`, so every *consequence* of a verdict happens in the scheduler. |
| `RunLog` | the harness's **authoritative** record | A Protocol, not the JSONL adapter, because the merge gate and scheduler both take one and orchestration may not name an adapter. |
| `CommandSource` | target repo → `RepoCommands` | Used during pre-flight readiness. `cli.py` names the concrete descriptor adapter and passes only the discovered value downstream. |
| `TestRunner` | a suite run → `SuiteResult` | The merge gate owns the single harness suite run, using `RepoCommands.test`. |
| `Git` | worktree / merge / ff plumbing | `merge_finished` is how the gate asks whether an Integrator finished — git's own `MERGE_HEAD`, not the session's word and not a commit count, because a session that resolves everything and never commits leaves a count that reads exactly as it would on success. `discard_worktree` destroys the checkout **and the branch** — `git worktree add -b` refuses an existing name, so a branch that outlived its checkout could never be cut again. The scheduler calls it on both non-quarantine exits: after a landing (the commits are on integration already) and on a `revise` verdict (losing them is the point). |

`Budget` ([ports.py](../ralph/ports.py)) carries the wall-clock backstop for an actor session.
`RepoCommands` is the command discovery value: the required `test` command and optional `install`
command, already shell-split.

**Revisions are stored alongside the Planner's original, never over it.** The revision number is the
*store's* to assign — an Editor choosing its own could overwrite an earlier one, and only the store
knows what is on disk. Revision 0 is what a human diffs against to see whether three cycles of Editor
rewriting quietly softened the spec (`docs/design.md` §8, the bet most likely to fail). Layout:

```
.scratch/<phase>/issues/
  01-sub.md                     ← the Planner's, live. Its Status: line is mirrored into it.
  revisions/01/
    0-spec.md  0-findings.md   ← snapshotted on the FIRST revision, never rewritten
    1-spec.md  1-findings.md   ← the Editor's
    2-spec.md  2-findings.md
```

Spec and findings are separate files because a revision may change one and leave the other alone.

---

## 4. Adapters — `ralph/adapters/`

All three unattended actors are chosen in `cli.py` from CLI arguments; nothing downstream knows which
concrete adapter is running, and `cli.py` remains the only module that names one.

| Vendor | Implementer transport | Integrator transport | Editor transport |
|---|---|---|---|
| **Codex** | `CodexJsonSession` over `codex exec --json` | the same, writable sandbox | `CodexJsonSession` over `codex exec --json` with `--sandbox read-only` |
| **Copilot** | `CopilotSdkSession` | the same, permitting writes | `CopilotSdkSession` with the SDK permission request hook |
| **Claude Code** | — | — | Claude Agent SDK session with `can_use_tool` |

The Integrator rides the Implementer's transport unchanged: both write code and commit it, so the
only thing that differs is the prompt. `runtime/integrator.py` is correspondingly the thinnest of the
three role cores — it collects no sentinel and parses no verdict, because the merge gate reads the
answer off git instead of out of the transcript.

The reason for keeping multiple vendors available on each unattended role lives in
[`docs/design.md`](design.md).

### Command descriptor adapter

The descriptor adapter ([commands.py](../ralph/adapters/commands.py)) reads `.ralph.toml` from the
target repo and returns `RepoCommands` through the `CommandSource` port. `cli.py` is the only place
that chooses that concrete adapter. Downstream code receives `RepoCommands`: `install_once()` gets
the optional `install` command, `SubprocessTestRunner` gets the required `test` command, and the
Editor allowlist receives that same `test` command.

### The wall-clock bound and token usage telemetry

`run_bounded(proc, budget)` ([bounding.py](../ralph/adapters/runtime/bounding.py)) is the kill loop every
adapter's `run`/`adjudicate` reduces to. It takes a `Killable` rather than a subprocess, because
turn-stream sessions and subprocesses are bounded through the same small interface.

Per-CLI usage details live in [`docs/cli-metering.md`](cli-metering.md). They are external to our
code and change on the vendors' schedule, not ours. What matters at this layer: token consumption is
telemetry, and no harness decision gates on it.

### Role cores sit above transports

`SubprocessImplementer` ([implementer.py](../ralph/adapters/runtime/implementer.py)) drives scripted
stand-ins and any process-only actor over the subprocess `Session` in
[session.py](../ralph/adapters/runtime/session.py). Vendor Implementers that shipped in Phase B use the same
telemetry core after `turn_stream.py` collects their output. Everything that makes an Implementer
session an Implementer session — counting commits, reading the diffstat, finding the `<impasse>`
sentinel — lives once in `implementer.py`, the twin of `editor.py`; each concrete adapter supplies
either an argv or a `TurnStreamSession`.
[prompt.py](../ralph/adapters/runtime/prompt.py) is the one place a `Spec` becomes
text a model reads, shared by Codex and Copilot so their failures stay comparable; findings go in as
a **separate section**, never folded into the spec.

### The Editor writes nothing but spec and findings — enforced, not asked

The Editor may read anything and run read-only commands; the moment it commits it is an Implementer
with a different name. **This is enforced, not prompted.** What counts as mutating is decided
**once**, in [editor.py](../ralph/adapters/runtime/editor.py) for SDKs with pre-tool permission callbacks
(the model-agnostic half also owns the `<verdict>` sentinel and the bounding). `ClaudeCodeEditor` is
that plus the Agent SDK's `can_use_tool` callback; `CopilotEditor` is that plus the SDK permission
request hook. Codex exposes no equivalent callback, so `CodexEditor` gets the same guarantee through
Codex's `read-only` OS sandbox instead.

Three things are load-bearing, each a hole in the obvious implementation:

- **It is an allowlist, not a blocklist.** A blocklist fails open on the tool nobody thought of — the
  one the SDK gains next week. `Write` is denied by *absence*.
- **Redirection and substitution are denied outright.** `git log > evidence.txt` passes any "is this
  read-only?" check — the write is in the shell, not the program — and it destroys the failed worktree
  a human was about to read. (For Copilot, *not* passing `--allow-all-tools` is what closes this hole.)
- **Every command in a pipeline is checked, not just the head.** `cat x | tee copy.py` begins harmless
  and ends as an Implementer.

The one thing the Editor may run that *executes* is the repo's own suite — the discovered
`RepoCommands.test`, passed in rather than guessed. `must_be_terminal` is surfaced **in the prompt**
and enforced **in the scheduler**; an Editor that returns no verdict classifies `infra-failed` and
the adapter returns `None` rather than inventing an `inconclusive` the parser fumbled into existence.

**`CopilotEditor`'s read-only guarantee is weaker than `ClaudeCodeEditor`'s** — not because the list
is shorter, but because `read_only()` is a pure function the harness owns and the suite attacks fifty
ways, while Copilot's enforcement lives inside an SDK we cannot inspect or test. The tests pin that
the harness *asks* correctly; that Copilot *honours* the ask is a reasonable assumption, still an
assumption. Prefer the callback-enforced Editor where the choice is free; Copilot exists so the
Editor need not be the same model as the Implementer, which matters more.

## 5. Orchestration

Depends on ports only, never on a concrete adapter.

### `MergeGate` — [mergegate.py](../ralph/mergegate.py)

Sub-issues run in parallel but **land one at a time**. `land(wt)` holds the merge lock (an
`asyncio.Lock`) for exactly: check the integration head has not moved → merge it in → **run the suite in
the worktree** → fast-forward. It returns a `Land(result, suite)`.

- The suite runs on the *prospective* merge result, so `merge --ff-only` is only ever a fast-forward
  of an already-verified tree: **the integration branch is correct by construction.**
- This is the single harness suite run. The scheduler does not run a post-session suite.
- The lock is never held while the Editor reasons, so one sub-issue's integration failure never
  stalls the gate for its siblings.
- **The gate writes nothing, anywhere.** Deciding a tree may become the integration branch is its
  job; recording *that* a sub-issue landed is a state transition, and those belong to the scheduler —
  two writers for one fact is one too many.
- `Land` carries the suite out because that prospective-merge suite is the **only honest one** for an
  `integration-failed` sub-issue: the worktree's own run was green (that is why it reached the gate),
  and handing the Editor a green `SuiteResult` beside an integration failure would be a manufactured
  contradiction.

### `Scheduler` — [scheduler.py](../ralph/scheduler.py)

One dispatch loop: read the graph once, then repeatedly dispatch every `eligible` sub-issue and
`await asyncio.wait(..., FIRST_COMPLETED)`.

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
- **The `editor` is always an `Editor`.** The CLI names a concrete Editor, `cli.py` resolves it
  before the scheduler starts, and any unknown name is a loud option failure. A failed
  Implementer session that routes to adjudication always reaches the Editor loop.
- **The scheduler never dispatches the Integrator.** The merge gate does, inside its own lock, and
  hands the session's telemetry back on the `Land`. The scheduler's only job for it is the one that
  was always the scheduler's: writing down that it happened and what it cost.

When a sub-issue escalates the run does **not** stop: everything transitively blocked by it never
becomes eligible, every unaffected sub-issue lands, and the run ends with **one** notification
(`docs/design.md` §4.7 for why quarantine-and-drain rather than fail-fast).

### `RunLog` and the two sinks — [runlog/](../ralph/runlog/)

Append-only, one line per event, two kinds of thing only: session states and Editor verdicts. Token
spend is deliberately not a run-log event; per-session consumption is persisted through the
`IssueStore`, and diffstats and failing-test output belong in the impasse report.

`cli.py` wraps the JSONL writer in a `NarratedRunLog`, so every event reaching the file also reaches
the terminal as it is written — file first, so the narration can never claim something the record
does not. It is a **view**, not a second sink: same fields, same order, same UTC clock. Between the
first session and the closing notification a run is otherwise silent for hours.

`Event` and `EventKind` live in [runlog/model.py](../ralph/runlog/model.py), outside `harness/`,
because they are the ledger vocabulary rather than a harness decision. `event()` lives beside them
because it reads the clock, and the harness core stays pure. `JsonlRunLog` is the concrete append-only
storage implementation in [runlog/jsonl.py](../ralph/runlog/jsonl.py). `ports.py` may import the
run-log value, but orchestration still depends on the `RunLog` Protocol rather than the JSONL
implementation. `Event` carries `actor` because a cycle closes **two** sessions against one
sub-issue — without it, `session-finished: infra-failed` could not say whether the Implementer or the
Editor crashed.

The run log (**authoritative** — failing to write it fails the run) and `IssueStore.write_event`
(**best-effort** — mirrors the transition into the tracker) are the same `Event` to two sinks of
different durability. Consumption is a separate issue-store record, not a third run-log event. A
cycle's worth reads as a story with two characters:

```
01  implementer  session-started    in-progress
01  implementer  session-finished   impasse
01  editor       session-started    in-progress
01  editor       session-finished   success     ← the EDITOR's session succeeded…
01  editor       verdict-recorded   revise      ← …and this is what it found
01  implementer  session-started    in-progress ← cycle two, against a rewritten spec
01  implementer  session-finished   success
01  implementer  sub-issue-closed   landed
```

`Transcripts` is the second sink for a finished session, alongside consumption: the scheduler writes
both together in `_record_session`, for all three actors, so no call site can record half of a
session. `FileTranscripts` in [transcripts.py](../ralph/transcripts.py) puts each one at
`<sub-issue>/<cycle>-<actor>.log`. The port is write-only by construction — the harness never reads
a transcript back, and one that could would be inviting a decision to be made out of model prose.
The Integrator's is written here too, not by the merge gate: the gate writes nothing, and
`Land.integrator` is how its telemetry reaches a writer.

What the scheduler writes is `SessionTelemetry.transcript`, and each `TurnStreamSession` fills it
at the point it reads its transport — the decoded line in `CodexJsonSession._run`, the SDK message
in `_SdkSession._converse`, the SDK event in `CopilotSdkSession._observe` — always on the line
*before* the interpreting one. `session_output` is the separate, parsed string the sentinels come
out of. Keeping the two apart is a design commitment, not a convenience; see
[design.md](design.md#the-transcripts).

### The pre-flight — [preflight.py](../ralph/harness/rules/preflight.py), gathered in `cli.py`

**It refuses; it does not warn.** Seven checks, each describing a run the harness would otherwise
damage or misjudge:

| Check | What it would otherwise do |
|---|---|
| `protected-branch` | Fast-forward `main`. Ralph lands onto the branch it is run from. |
| `uncommitted-changes` | Fight the merge gate's fast-forwards over uncommitted work, and lose. |
| `uninstalled-pre-commit-hooks` | Land commits that skipped the checks the repo believes it enforces. |
| `missing-test-command` | Start without a suite command the merge gate can run. |
| `missing-actor-runtime` | Start with an actor whose CLI or SDK is absent, and discover it at the session that needed it. |
| `invalid-issue-source` | Start against an issue source it cannot reach or was misconfigured to find. |
| `invalid-issue-graph` | Read a source that read fine but holds a graph it cannot use. |

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
| Merge gate | [mergegate.py](../ralph/mergegate.py) |
| Failure taxonomy + suite gate | [classify.py](../ralph/harness/rules/classify.py), [routing.py](../ralph/harness/rules/routing.py), [runlog/](../ralph/runlog/), [mergegate.py](../ralph/mergegate.py) |
| Command discovery | `CommandSource`, `RepoCommands`, [commands.py](../ralph/adapters/commands.py), `cli.command_source_for()` |
| Wall-clock bound and usage telemetry | `Budget`, [bounding.py](../ralph/adapters/runtime/bounding.py), per-CLI usage parsing ([cli-metering.md](cli-metering.md)) |
| Impasse report format | [impasse.py](../ralph/harness/model/impasse.py), [failure.py](../ralph/harness/model/failure.py) |
| The Editor | [claude/actors.py](../ralph/adapters/claude/actors.py), [codex/actors.py](../ralph/adapters/codex/actors.py), [copilot/actors.py](../ralph/adapters/copilot/actors.py), `CycleLedger` |
| Linear sync | `issues/linear/` behind the existing `IssueStore` Protocol |
| Pre-flight + notification | [preflight.py](../ralph/harness/rules/preflight.py), [notification/](../ralph/notification/), `RunReport`, `cli.render` |
