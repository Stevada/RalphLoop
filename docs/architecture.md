# Ralph Loop — Harness Architecture

The Python harness. This document specifies the modules, types, and seams; `docs/prd.md`
specifies *why*, and [`UBIQUITOUS_LANGUAGE.md`](../UBIQUITOUS_LANGUAGE.md) is canonical for
every term used here. Identifiers in this codebase use canonical terms only — an "aliases to
avoid" word appearing as a class, function, field, or state name is a defect.

## Why Python

*Historical note. The bash prototype this argument refers to has been deleted; nothing in the
repo depends on it, and nothing below asks you to port anything. It is recorded because it is
the reason the system has the shape it does.*

The bash prototype was good at what it did: worktrees, rebase, fast-forward, lock. That is git
plumbing, and bash is the right language for it. But every remaining item in PRD §10 — the
failure taxonomy, the 120k ceiling, the impasse report, the Editor, Linear sync — is a thing
bash is structurally bad at, and the ceiling in particular requires consuming a JSONL event
stream and killing a process mid-session. The mix had flipped: git plumbing was the minority
of the code.

One consequence is worth stating up front, because it deletes code. A **run** is a single
process: the merge lock is an `asyncio.Lock` rather than `flock`. No PID tracking, no result
files, no polling to reap finished sessions. If you find yourself reaching for any of those,
you have mistranslated the design.

## Layers

The dependency arrow points inward, always.

```
ralph/
  domain/          pure. no I/O. stdlib only.
    __init__.py    ← THE INTERFACE. import `from ralph.domain import Outcome`, never from
                     `ralph.domain.model.session`. the layout below is nobody else's business.
    model/         the NOUNS — frozen values, zero logic. what the system is made of.
      graph.py     SubIssueId, SubIssue, IssueGraph, GraphError
      content.py   Brief, Findings
      session.py   Actor, Outcome, SessionTelemetry, SuiteResult
      impasse.py   Approach, ImpasseReport        ← the model's claim
      failure.py   FailureReport                  ← the claim + the harness's facts
      verdict.py   Verdict, EditorVerdict
      state.py     SubIssueState
      event.py     Event, EventKind
      notification.py  Escalation, Notification   ← the one thing a human reads afterwards
    rules/         the VERBS — pure functions. what the system DECIDES.
      classify.py     session → Outcome            (the failure taxonomy)
      routing.py      (actor, outcome) → Destination   (and never a retry)
      eligibility.py  graph + states → what may run    (quarantine-and-drain)
      cycles.py       CycleLedger                      (the cap of three)
      report.py       → FailureReport      (and the impasse it will NOT invent)
      notify.py       → Notification       (what each failure cost, and what to open first)

  ports.py         Protocols — the seams. every one has a fake.
  adapters/        codex, copilot, claude_editor, context, prompt, session,
                   git, filesystem, runlog, suite
  events.py        the Event factory — needs a clock, so not domain/; needed by both
  mergequeue.py    \  the merge queue and the scheduler, so not an adapter either
  scheduler.py      } orchestration — depends on ports only, never on a concrete adapter
  cli.py           composition root — the only place a concrete adapter is named

tests/
  fakes.py         a fake per Protocol. adapters: they satisfy an interface at a seam.
  builders.py      telemetry(), graph_of(), … values, not adapters. a different thing.
  testbed.py       a REAL git repo and a REAL stand-in agent subprocess. neither is a fake.
```

The **stub Editor** is `FakeEditor` — scripted verdicts, no model. The Editor loop is proven before
any model is involved, and #09 swaps in the real one without touching a line of the scheduler.

**`rules/` is the harness.** Six files, and between them they hold the entire design: the failure
taxonomy, the fact that nothing is ever retried, how quarantine drains, the cycle cap, the report
the harness will not fabricate, and which failure a human should open first. Everything else in
this repo exists to feed them. If you want to know what this system *decides*, you read one folder.

**`rules/` may import `model/`. `model/` may not import `rules/`** — a test enforces it. A value
that knows how it will be classified has stopped being a value.

The domain is **not** split by actor, and that is deliberate. `Outcome`, `SessionTelemetry`, and
`SuiteResult` belong to *both* actors — the Editor is bounded exactly like the Implementer — and
`route(actor, outcome)` is explicitly about both. An `implementer/` ⁄ `editor/` split would leave
the shared types with no owner and force a `shared/` folder that ends up holding most of the
domain. The same objection is why `adapters/` is flat: `copilot.py` is *both* an Implementer and
an Editor, so grouping adapters by port would have to split or duplicate it.

**The fakes live under `tests/`, not in the package.** Ralph is an application: nothing downstream
imports `ralph.fakes`, and keeping the fakes out of the package is what makes "no adapter may reach
for a fake" enforceable rather than aspirational. A test asserts the package imports nothing from
`tests/`.

The Implementer and the Editor are each chosen at startup — Codex or Copilot implements;
Claude Code or Copilot edits — and nothing downstream of `cli.py` knows which.

The hard logic — classification, routing, eligibility, the cycle cap — lives in `domain/`
as pure functions over frozen dataclasses. It is tested with no subprocess, no git, and no
model.

---

## 1. Domain

### Structure vs. content

A **sub-issue** is immutable for the life of a **run**. Its **brief** and **findings** are
not: the **Editor** rewrites them. So structure is a frozen value and content lives behind a
store. This is what makes the "the Editor may never add, remove, or re-link a sub-issue"
invariant a property of the types rather than a rule someone has to remember.

```python
# domain/model/graph.py

SubIssueId = NewType("SubIssueId", str)

@dataclass(frozen=True, slots=True)
class SubIssue:
    id: SubIssueId
    title: str
    blocked_by: frozenset[SubIssueId]
```

There is no `kind`. The Planner reasons about contract, implementation, and integration
roles when it *builds* the graph; once the graph exists those roles are fully encoded in the
`blocked by` edges, and no runtime actor reads them. A `kind` field would be a second,
un-checkable source of truth for something the edges already say.

```python
@dataclass(frozen=True, slots=True)
class IssueGraph:
    """Read once, at run start. Never mutated."""
    sub_issues: Mapping[SubIssueId, SubIssue]

    def __post_init__(self) -> None: ...      # raises on cycles and dangling edges
    def blockers_of(self, id: SubIssueId) -> frozenset[SubIssueId]: ...
    def transitively_blocked_by(self, id: SubIssueId) -> frozenset[SubIssueId]: ...
```

### Brief and findings are two fields, deliberately

```python
# domain/model/content.py

@dataclass(frozen=True, slots=True)
class Brief:
    """What "done" means. Acceptance criteria in prose, including tests as prose.
    This is the thing a human diffs against the Planner's original intent, and the thing
    that softens under the spec-drift bet."""
    body: str
    revision: int = 0        # 0 is the Planner's original and is never overwritten

@dataclass(frozen=True, slots=True)
class Findings:
    """What the last session learned about the repo — "the client's retry logic swallows
    the expected error." Difficulty-neutral by intent: a channel for adding information
    *without* lowering the bar. Kept out of the brief so the brief stays clean as spec."""
    body: str
```

### Session outcomes

The harness's classification of how *any* session ended. `impasse` and `silent-red` can only
come from an Implementer session — an Editor session cannot declare itself stuck — so there
are two classifiers, and the Editor's one is structurally incapable of returning them.

```python
# domain/model/session.py

class Actor(StrEnum):
    IMPLEMENTER = "implementer"
    EDITOR      = "editor"

class Outcome(StrEnum):
    SUCCESS            = "success"
    IMPASSE            = "impasse"
    SILENT_RED         = "silent-red"
    INTEGRATION_FAILED = "integration-failed"
    CEILING_EXCEEDED   = "ceiling-exceeded"
    INFRA_FAILED       = "infra-failed"

@dataclass(frozen=True, slots=True)
class SessionTelemetry:
    """The harness's word for everything the model cannot observe about itself."""
    exit_code: int
    killed: Literal["ceiling", "wall-clock"] | None
    peak_context_tokens: int                 # the ceiling is on THIS
    consumed_tokens: int                     # telemetry only. nothing is gated on it.
    wall_clock_s: float
    commits: int
    diffstat: str
    session_output: str                      # the session's own transcript — NOT the suite's.
                                             # the suite's output belongs to SuiteResult.output,
                                             # and a FailureReport carries both.
    impasse_report: ImpasseReport | None     # parsed from the <impasse> sentinel, out of the above

@dataclass(frozen=True, slots=True)
class SuiteResult:
    green: bool                              # never `verified`, `trusted`, or `passing`
    output: str
    duration_s: float
```

`SuiteResult.green` is deliberately not called `verified`. A suite run by the harness runs
**inside the blast radius**; only CI on a clean checkout is **honest**. Holding that line in
the identifiers is free, and the day someone writes `if suite.verified:` is the day the
distinction starts to erode.

```python
# domain/rules/classify.py

def classify_implementer(t: SessionTelemetry, suite: SuiteResult) -> Outcome:
    """Precedence is load-bearing: a ceiling kill and a crash both exit non-zero and are
    indistinguishable downstream unless they are separated here."""
    if t.killed == "ceiling":                            return Outcome.CEILING_EXCEEDED
    if t.killed == "wall-clock" or t.exit_code == 124:   return Outcome.INFRA_FAILED
    if t.impasse_report is not None:                     return Outcome.IMPASSE
    if t.commits == 0:                                   return Outcome.SILENT_RED
    if not suite.green:                                  return Outcome.SILENT_RED
    return Outcome.SUCCESS

def classify_editor(t: SessionTelemetry, verdict: EditorVerdict | None) -> Outcome:
    """Editor success is a verdict returned. Cannot yield IMPASSE or SILENT_RED."""
    if t.killed == "ceiling":                            return Outcome.CEILING_EXCEEDED
    if t.killed == "wall-clock" or t.exit_code == 124:   return Outcome.INFRA_FAILED
    if verdict is None:                                  return Outcome.INFRA_FAILED
    return Outcome.SUCCESS
```

Zero commits is never a benign skip. `INTEGRATION_FAILED` is unreachable from either
classifier: it does not classify a session — the Implementer session already succeeded, green
in isolation — and only the merge queue can raise it.

### The claim and the corroboration

The impasse report is the model's narration. `SessionTelemetry` is what the harness observed. They
are separate types, and a `FailureReport` puts them side by side — because the Editor's job is to
check one against the other, and *their disagreeing is itself a signal*.

```python
# domain/model/impasse.py — a leaf. What the session emits; it knows nothing about how it was classified.

@dataclass(frozen=True, slots=True)
class Approach:
    tried: str
    abandoned_because: str

@dataclass(frozen=True, slots=True)
class ImpasseReport:
    failing_test: str
    assertion_output: str                 # verbatim, not summarised
    approaches: tuple[Approach, ...]
    unsatisfiable_criterion: str
    what_would_satisfy: str

# domain/model/failure.py — composes the claim with the harness's facts, so it sits downstream
# of `session.py`, which imports `impasse.py`. Splitting them is what breaks that import cycle.

@dataclass(frozen=True, slots=True)
class FailureReport:
    outcome: Outcome
    claim: ImpasseReport | None    # absent for silent-red (it believed it had succeeded) and for
                                   # integration-failed (it *had* succeeded; the queue rejected it)
    telemetry: SessionTelemetry    # never absent
    suite: SuiteResult
    integration_detail: str | None = None    # the merge queue's record, when it raised the failure
```

### Routing

The taxonomy table, executable, one test per row. `ceiling-exceeded` and `infra-failed` route
identically for both actors; only `SUCCESS` needs to know who is asking, because an
Implementer's success goes to the merge queue and an Editor's success is a verdict to act on.

```python
# domain/rules/routing.py

class Destination(StrEnum):
    MERGE_QUEUE    = "merge-queue"
    ACT_ON_VERDICT = "act-on-verdict"
    EDITOR         = "editor"
    HUMAN          = "human"
    # There is no retry destination. There is no retry anywhere in this system.

def route(actor: Actor, outcome: Outcome) -> Destination:
    match (actor, outcome):
        case (Actor.IMPLEMENTER, Outcome.SUCCESS):
            return Destination.MERGE_QUEUE
        case (Actor.EDITOR, Outcome.SUCCESS):
            return Destination.ACT_ON_VERDICT
        case (Actor.IMPLEMENTER, Outcome.IMPASSE | Outcome.SILENT_RED):
            return Destination.EDITOR
        case (_, Outcome.INTEGRATION_FAILED):
            return Destination.EDITOR
        case (_, Outcome.CEILING_EXCEEDED | Outcome.INFRA_FAILED):
            return Destination.HUMAN     # both, from either actor. never the Editor, never a retry.
```

**`CEILING_EXCEEDED` and `INFRA_FAILED` are the two outcomes the Editor never sees, and neither
is ever retried.** Both go straight to `needs-human` with the worktree preserved, and both
**spend no cycle** — a cycle is an Implementer session plus the Editor session that follows it,
and no Editor is involved in either.

For `CEILING_EXCEEDED`, the remedy is a **re-cut of the graph**, which is exactly what the Editor
is forbidden to make (it rewrites a brief; it never adds, removes, or re-links a sub-issue). The
only move it would have is to soften the brief until the work fits — the spec-drift failure mode
arriving dressed as a remedy.

For `INFRA_FAILED`, the failure is in the environment, not the work. A stale lockfile, a 429, an
OOM, a wall-clock kill: none is fixed by running the same session again against the same broken
environment. Retrying burns the budget, delays the notification, and produces a second failure
identical to the first, because the model cannot see the cause. **There is no retry anywhere in
this system** — a test should assert that no `route()` row returns anything but the four
destinations above.

The two keep separate identities despite sharing a destination because they hand the human
different diagnoses: *the sub-issue was cut too large* versus *your environment is broken*. One
sends you to the graph, the other to the lockfile.

### Verdicts and the cycle cap

```python
# domain/model/verdict.py  (the types)  +  domain/rules/cycles.py  (the cap)

class Verdict(StrEnum):
    REVISE          = "revise"
    PLANNING_DEFECT = "planning-defect"
    INCONCLUSIVE    = "inconclusive"

    @property
    def is_terminal(self) -> bool:
        return self is not Verdict.REVISE

@dataclass(frozen=True, slots=True)
class EditorVerdict:
    verdict: Verdict
    revised_brief: Brief | None        # required iff REVISE
    revised_findings: Findings | None
    rationale: str

@dataclass(slots=True)
class CycleLedger:
    """A cycle is one Implementer session plus the Editor session that follows it.
    At most three per sub-issue, hard-enforced: the harness dispatches no fourth
    Implementer session, whatever the Editor says."""
    MAX_CYCLES: ClassVar[int] = 3
    _spent: dict[SubIssueId, int] = field(default_factory=dict)

    def spend(self, id: SubIssueId) -> int: ...
    def exhausted(self, id: SubIssueId) -> bool: ...
    def must_be_terminal(self, id: SubIssueId) -> bool:
        """True on the final cycle. The Editor may not return `revise`; the harness
        rejects it rather than trusting the Editor to remember the rule."""
```

### Lifecycle

```python
# domain/model/state.py  (the type)  +  domain/rules/eligibility.py  (the rules)

class SubIssueState(StrEnum):
    READY       = "ready"          # stored: the Planner authorised this sub-issue to run
    IN_PROGRESS = "in-progress"
    LANDED      = "landed"         # terminal: fast-forwarded into the integration branch
    NEEDS_HUMAN = "needs-human"    # quarantine: planning-defect or inconclusive

def eligible(graph: IssueGraph, states: Mapping[SubIssueId, SubIssueState]) -> frozenset[SubIssueId]:
    """Derived, never stored: every sub-issue this one is blocked by has LANDED."""

def never_eligible(graph, states) -> frozenset[SubIssueId]:
    """Report-time only. A sub-issue still READY at run end whose blockers never landed
    never got a turn."""
```

There is **no** state for a sub-issue whose upstream escalated. It stays unstarted with its
`blocked by` edge intact, which is already distinct from `needs-human` — "I failed" and "I
never got a turn" are distinguishable without inventing a state for the second. `never_eligible`
is derived at report time; nothing propagates a skip through the graph.

`LANDED` is a sub-issue's terminal state. `done` is the **parent issue's** terminal state and
is banned here.

---

## 2. Ports

Every protocol has a fake, and the fakes are what the test suite runs against.

```python
# ports.py

@dataclass(frozen=True, slots=True)
class Budget:
    """Two bounds that catch different failures; neither substitutes for the other.

    max_context_tokens — the smart zone. A quality bound, not a cost cap: a model
    reasoning over 200k of context is a worse engineer than the same model over 100k.
    One number for both actors, regardless of which model runs.

    wall_clock_s — the backstop, and the thing that catches a *stuck* session. A session
    re-running a failing suite twenty times has a flat context and will never trip the
    ceiling; only the clock stops it.
    """
    max_context_tokens: int = 120_000
    wall_clock_s: float = 1800.0

class Implementer(Protocol):
    async def run(self, brief: Brief, findings: Findings,
                  worktree: Worktree, budget: Budget) -> SessionTelemetry: ...

class Editor(Protocol):
    async def adjudicate(self, brief: Brief, findings: Findings,
                         failure: FailureReport, worktree: Worktree,
                         budget: Budget, must_be_terminal: bool,
                         ) -> tuple[SessionTelemetry, EditorVerdict | None]: ...

class IssueStore(Protocol):
    """The issue tracker: sub-issue files today, Linear later."""
    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]: ...
    def content(self, id: SubIssueId) -> tuple[Brief, Findings]: ...   # the NEWEST revision
    async def record_revision(self, id, brief: Brief, findings: Findings) -> None: ...
    async def write_event(self, e: Event) -> None: ...    # best-effort: a run must not die
                                                          # because Linear was unreachable

class RunLog(Protocol):
    """The harness's authoritative record. A port, not a concrete JSONL writer, because the
    merge queue and the scheduler both take one and orchestration may not name an adapter."""
    async def write(self, e: Event) -> None: ...
    def events(self) -> tuple[Event, ...]: ...

class TestRunner(Protocol):
    async def run(self, dir: Path) -> SuiteResult: ...

class Git(Protocol):
    def add_worktree(self, branch: str, at: Path, base: str) -> Worktree: ...
    def rebase(self, wt: Worktree, onto: str) -> bool: ...
    def merge_ff_only(self, branch: str) -> bool: ...
    def commits_between(self, base: str, branch: str) -> int: ...
    def head_branch(self) -> str: ...
```

The Editor is bounded exactly like the Implementer: same `Budget`, same `SessionTelemetry`,
and it can come back `ceiling-exceeded`.

`Git.discard_worktree` is what "the Implementer's work is discarded" means in git: it destroys the
checkout **and the branch**. Deleting the branch is not tidiness — the next cycle re-cuts
`ralph/<id>` from the integration branch, and `git worktree add -b` refuses a branch that already
exists. Leaving it would either abort the restart or, worse, silently resume from the abandoned
work, which is the one thing "restarts clean" exists to prevent.

**Revisions are stored alongside the Planner's original, never over it.** The revision number is
the *store's* to assign — an Editor that could choose its own could overwrite an earlier one, and
only the store knows what is already on disk.

```
.scratch/<phase>/issues/
  01-sub.md                     ← the Planner's, live. Its `Status:` line is mirrored into it.
  revisions/01/
    0-brief.md  0-findings.md   ← snapshotted on the FIRST revision, never rewritten
    1-brief.md  1-findings.md   ← the Editor's
    2-brief.md  2-findings.md
```

Revision 0 is what a human diffs against to see whether the spec **drifted** — whether three rounds
of Editor rewriting quietly softened "reject the request" into "log a warning". That is the design
bet in `docs/prd.md` §8 most likely to fail, and a harness that rewrote the brief in place would
destroy the evidence with the very mechanism under suspicion. Brief and findings are separate files
because they are separate ideas: a revision may change one and leave the other alone.

---

## 3. Adapters

Two CLIs, and **either can back either actor**. Codex or Copilot as the Implementer; Claude
Code or Copilot as the Editor. Chosen in `cli.py` from `RALPH_IMPLEMENTER` / `RALPH_EDITOR`;
nothing else in the system knows which is running.

|  | Implementer | Editor |
|---|---|---|
| **Codex** (`codex exec`) | ✅ | — |
| **Copilot** (`copilot -p`) | ✅ | ✅ |
| **Claude Code** (`claude-agent-sdk`) | — | ✅ |

That is a real portfolio decision, not a hedge: the Implementer and the Editor should not be
the same model on the same failure. An Editor adjudicating an impasse declared by *itself* is
the least independent sensor the system could have. Keeping two CLIs on each side keeps that
choice available.

### The ceiling seam

The smart-zone kill is identical for every adapter, but each CLI publishes its context
differently. So the ceiling logic lives once, behind a seam:

```python
# ports.py

@dataclass(frozen=True, slots=True)
class Observation:
    """One model call, as the CLI recorded it."""
    context_tokens: int                     # THE CEILING IS ON THIS, AND ONLY THIS
    consumed_tokens: int                    # cumulative spend. telemetry only.
    rate_limit_used_percent: float | None   # logged, never gated

class ContextSource(Protocol):
    """Yields an observation per model call, live, while a session runs."""
    def observations(self) -> AsyncGenerator[Observation, None]: ...
```

The three numbers travel together in one value because **that is how they arrive**: every CLI
reports context and consumption in the *same* event, adjacent, with names that read alike
(`last_token_usage` beside `total_token_usage`; `prompt_tokens` beside `total_tokens`). A ceiling
wired to the wrong one is not merely inaccurate — it is *inverted*. It kills a long, cheap,
tightly-focused session and waves through a bloated one.

`AsyncGenerator` and not merely `AsyncIterator`, because **closing is part of the contract**: a
source that tails a file holds a file handle open, and the ceiling kill breaks out of the loop
mid-stream. `run_bounded` closes it; the type is what obliges it to.

```python
# adapters/context.py

class ContextMeter:
    """Watches context, not consumption. Records the high-water mark — whether or not it
    tripped — and trips when the smart zone is left. Shared by every adapter."""
    def observe(self, o: Observation) -> None: ...
    @property
    def peak(self) -> int: ...
    @property
    def exceeded(self) -> bool: ...      # reads `peak`, NOT the last observation

async def run_bounded(proc, source: ContextSource | None, budget: Budget) -> Bound:
    """The kill loop. Every adapter's `run`/`adjudicate` is this plus prompt rendering."""
    meter, killed = ContextMeter(budget.max_context_tokens), None
    try:
        async with asyncio.timeout(budget.wall_clock_s):
            if source is not None:
                async with aclosing(source.observations()) as observations:
                    async for o in observations:
                        meter.observe(o)
                        if meter.exceeded:
                            killed = "ceiling"; break
            if killed is None:
                await proc.wait()      # the source runs dry before the process exits
    except TimeoutError:
        killed = "wall-clock"          # the stuck-session catcher
    if killed is not None:
        proc.kill()
    return Bound(killed, meter.peak, meter.consumed)
```

`exceeded` reads the **peak**, not the last observation: a model that touched 130k and then
compacted back to 90k has already done its bad thinking, and the compaction must not be allowed to
hide the crossing.

`source=None` is not "unbounded" — it is *a session publishing no context signal*: the stand-in
agent, or any bare `RALPH_AGENT_CMD`. It is bounded on the clock alone, and its peak is honestly
reported as zero rather than invented.

### An Implementer is an argv and a context source

That is the whole of `SubprocessImplementer`, and it is why `codex.py` is a hundred lines. Codex is
`codex exec` plus a rollout tail; Copilot is `copilot -p` plus a debug-log tail; the stand-in agent
is an argv and nothing. Everything that makes a session a session — both bounds, counting the
commits, reading the diffstat, finding the `<impasse>` sentinel — lives once in `session.py`.

`adapters/prompt.py` is the one place a `Brief` becomes text a model reads. Codex and Copilot are
different argv and the *same job*: a prompt that drifted between them would make their failures
incomparable, which is the one thing the harness exists to prevent. The findings go in as a
**separate section**, never folded into the brief — they add information; the brief sets the bar,
and merging them is how a bar gets lowered by accident.

### `CodexImplementer` — `adapters/codex.py`

**The context signal is not on stdout.** `codex exec --json` emits `turn.completed` — carrying
`usage` — only at session end, because one `exec` invocation is a single turn, tool calls
included. Verified against the CLI. What *is* live is the session **rollout file**
(`~/.codex/sessions/<date>/rollout-*.jsonl`), to which Codex appends a `token_count` event
after **every model call**:

```json
{"type": "event_msg", "payload": {"type": "token_count", "info": {
  "last_token_usage":  {"input_tokens": 16802},   ← the context on the last call. THE CEILING.
  "total_token_usage": {"total_tokens": 33410},   ← consumption. telemetry only.
  "model_context_window": 272000
}, "rate_limits": {"primary": {"used_percent": 0.0}}}}
```

So `CodexContextSource` tails the rollout file, not stdout, yielding
`info.last_token_usage.input_tokens`. The `thread_id` from `thread.started` on stdout
identifies which rollout file belongs to this session — and that identification is not a nicety.
Four Codex sessions run at once by default, each appending to its own rollout file in the same
directory. Metering a sibling's file would kill the wrong session, and the harness would report
`ceiling-exceeded` against a sub-issue that never left the smart zone.

Stdout is therefore needed for exactly one fact, live. `Transcript` is what serves it: it
accumulates the whole session output for telemetry, *and* republishes lines as they arrive until a
`ContextSource` has found what it came for — after which it stops republishing, so nothing
accumulates behind a consumer that has lost interest.

The ceiling (120k) sits far below the window (272k), so it always fires **before** Codex would
auto-compact — compaction never gets the chance to drop the context back under the bound and
hide the crossing. `rate_limits.primary.used_percent` rides along in the same event: free early
warning for the 429 → `infra-failed` case. Log it; don't gate on it yet.

### `CopilotImplementer` — `adapters/copilot.py`

`copilot -p <prompt> --model <m> --log-dir <fresh> --log-level debug --no-color
--disable-builtin-mcps --disable-mcp-server <each> --allow-all-tools`.

**Copilot does have an event stream, and it is useless to us.** `--output-format json` emits
clean JSONL — and the only token it ever publishes is `outputTokens`, the *completion* count.
It never mentions the prompt. It is precisely the number the ceiling does not want. The context
lives in the **debug log** and nowhere else:

```json
"usage": {
  "prompt_tokens": 25885,        ← the context on this call. THE CEILING.
  "completion_tokens": 4,
  "total_tokens":  25889,        ← what THIS CALL spent. NOT a running total.
  "prompt_tokens_details": { "cached_tokens": 0, "cache_creation_tokens": 25883 }
}
```

`CopilotContextSource` tails the log and yields `usage.prompt_tokens`. Four things differ from
Codex, and every one of them is a way the ceiling silently stops working:

- **`--log-level debug` is required.** At the default level there are no `usage` blocks at all —
  thirty-five logs on the dev machine, not one with a token count in it. A session that publishes
  no observation is not a cheap session, it is an **unmetered** one, so the adapter *raises* rather
  than let it run to the wall clock unwatched.
- **The log is pretty-printed JSON inside a timestamped text log, not JSONL.** It needs a
  brace-matched block extractor, counting braces **outside string literals only** — the blocks
  embed the whole prompt, and the prompt embeds the brief. One `if (x) {` in a target repo's
  acceptance criteria is an unbalanced brace inside a JSON string, and a naïve counter never finds
  the end of the block.
- **The block must be parsed before it is trusted.** The same log carries a model-capabilities
  block containing `max_prompt_tokens: 200000`. Anything that went looking for the *string*
  `prompt_tokens` would read the model's context **limit** as its context **usage** and kill every
  Copilot session ever run, before its first turn. `usage` is read from the top level of the
  parsed object.
- **`total_tokens` is per-call, where Codex's `total_token_usage` is cumulative** (25,885 + 4 =
  25,889, and the next call starts over). The adapter accumulates, because `ContextMeter` takes a
  `max()` over what it is handed and would otherwise report the largest single call as the whole
  session's spend.

**Copilot loads the world into the prompt before it reads the brief.** Measured, one word in and
one word out:

| launched | context to answer "pong" | of the smart zone |
|---|---|---|
| default | **56.5k** | 47%, gone at turn zero |
| MCP disabled | **25.9k** | 22% |
| MCP disabled + read-only tool allowlist | **8.4k** | 7% |

So the harness disables MCP on every Copilot run — a ceiling that fires on work which never had
room to begin with is not a quality bound, it is a tax. **`--disable-builtin-mcps` is not enough
on its own:** it disables `github-mcp-server` and nothing else, while the servers actually costing
us the prompt come from the user's config, the workspace, and installed plugins. The harness asks
`copilot mcp list --json` and disables each by name, because it cannot guess names it has never
seen.

Codex, by comparison, starts at ~16k and needs none of this.

Unlike Codex, a Copilot session has **no thread id to announce and no shared directory to find
itself in**: it is handed a private `--log-dir`, emptied first — a revised cycle re-cuts the same
branch at the same path, so last cycle's log would otherwise be sitting exactly where this cycle's
is about to be looked for. There is nothing to disambiguate, so there is no stdout handshake.

`--max-ai-credits` is a *credit* cap, not a context cap. It is not a substitute for the ceiling.

### `ClaudeCodeEditor` — `adapters/claude_editor.py`

The Editor writes only the brief and findings. It may read anything and run read-only commands.
It never commits, never cherry-picks, never touches a worktree except to read it — the moment it
commits it is an Implementer with a different name. **That is enforced by the tool allowlist,
not by the prompt.**

What counts as mutating is decided **once**, in `adapters/editor.py` — the model-agnostic half of
an Editor, which also owns the `<verdict>` sentinel and the bounding. `ClaudeCodeEditor` is that
plus the Agent SDK; `CopilotEditor` is that plus `--deny-tool` flags. Only the *delivery* of the
denial differs.

```python
# adapters/editor.py

READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash"})
READ_ONLY_GIT   = frozenset({"diff", "log", "show", "status", "grep", "blame", ...})
FORBIDDEN_SHELL = ("&", ">", "<", "$(", "`")

def read_only(tool, input, suite) -> Permission: ...   # THE ENFORCEMENT SURFACE
```

Three things are load-bearing, and each is a hole in the obvious implementation:

- **It is an allowlist, not a blocklist.** A blocklist fails open, and it fails open on precisely
  the tool nobody thought of — the one the SDK gains next week. `Write` is denied by *absence*, and
  so is `SomeToolAddedIn2027`.
- **Redirection and substitution are denied outright.** `git log > evidence.txt` passes any check
  that asks "is this a read-only command?", because it *is* one — the write is in the shell, not in
  the program, and it destroys the failed worktree a human was about to read.
- **Every command in a pipeline is checked, not just the head.** `cat calculator.py` is as harmless
  as a command gets; `cat calculator.py | tee copy.py` begins that way and ends as an Implementer.

The one thing the Editor may run that *executes* is **the repo's own suite** — the same command the
harness itself runs, passed in rather than guessed at. That is how "re-run the suite in the failed
worktree" and "you may not write to it" are both true at once.

`must_be_terminal` is surfaced **in the prompt** and enforced nowhere near it: the **scheduler**
refuses a third-cycle `revise`. One rule, one home. An adapter that also rejected it would be a
second home for the cycle cap, and one day the two would disagree.

An Editor that returns **no verdict** classifies `infra-failed`, and the adapter returns `None`
rather than inventing one. The temptation is to "repair" an unreadable verdict into an
`inconclusive` — terminal, safe-feeling, pages a human. It is not safe: downstream a fabricated
`inconclusive` is indistinguishable from a considered one, and the harness would be putting an
opinion in the Editor's mouth when the Editor may have said `planning-defect` in words the parser
fumbled.

The SDK streams usage per message, so its `ContextSource` needs no log tailing — the session's
`turns()` yield text and `Observation`s interleaved, and `run_editor` splits them. The Editor is
bounded by the same `run_bounded` as an Implementer, which is why that function takes a `Killable`
rather than a subprocess: the SDK conversation is not a process at all.

### `CopilotEditor` — `adapters/copilot.py`

Same contract, delivered by the CLI's own permission engine rather than an SDK callback:

```
--available-tools=view,glob,grep,bash     ← an ALLOWLIST. `create` and `edit` denied by absence.
--deny-tool=write                         ← denial beats every allow, including --allow-all-tools
--allow-tool='shell(git diff)' ...        ← one per read-only command; the suite gets a prefix match
                                          ← and NO --allow-all-tools, which is itself enforcement
```

Three of those four lines are load-bearing, and the fourth is the *absence* of a line:

- `--available-tools` is an allowlist and Copilot honours it by **never sending the other tools to
  the model** — verified against a real session's wire request, where `create` and `edit` simply
  were not there. Same shape as the SDK Editor's, for the same reason: a blocklist fails open on
  the tool nobody thought of.
- **Not passing `--allow-all-tools` is part of the enforcement.** Without it Copilot denies any
  shell command not allowed by name, and refuses shell redirection outright — which is what closes
  the `git log > evidence.txt` hole that no per-command check can see, because the write is in the
  shell rather than the program.
- What counts as read-only is **not redefined here**. `READ_ONLY_COMMANDS` and `READ_ONLY_GIT` are
  the same frozensets the SDK Editor's permission callback consults, in `adapters/editor.py`; this
  adapter only translates them into `shell(...)` patterns. Two Editors, one definition — the day
  they drifted, the two would no longer be running under the same rules.

**This Editor's read-only guarantee is weaker than `ClaudeCodeEditor`'s, and the difference is not
that one list is shorter.** It is that `read_only()` is a pure function the harness owns and the
suite attacks fifty ways, while this one's enforcement lives inside a binary we do not control,
cannot inspect, and do not exercise in any test. What the tests here pin is that the harness *asks*
correctly. That Copilot then *honours* the ask is an assumption — a reasonable one, and still an
assumption. Prefer the SDK Editor where the choice is free; this exists so that the Editor need not
be the same model as the Implementer, which matters more.

Same ceiling caveat as `CopilotImplementer` — and the same 8.4k baseline, since the tool allowlist
is most of what buys it back.

---

Reproduction, not inference. Since declaring an impasse is cheap, the Editor's ability to check
the Implementer's story against the repository is the only thing standing between us and a
system where declaring an impasse always works. That holds whichever CLI backs it.

### Stores

`FilesystemIssueStore` reads `.scratch/<phase>/issues/*.md` with numeric-prefix edges, as
today. `LinearIssueStore` implements the same protocol and changes nothing in the scheduler —
which is how PRD gaps #1 and #13 collapse into "write a second class."

---

## 4. Orchestration

### `MergeQueue`

Sub-issues run in parallel but **land one at a time**. One process now, so the merge lock is
an `asyncio.Lock`.

```python
# mergequeue.py

class LandResult(StrEnum):
    LANDED          = "landed"
    REBASE_CONFLICT = "rebase-conflict"    # ─┐
    SUITE_RED       = "suite-red"          #  ├─ all three are Outcome.INTEGRATION_FAILED
    FF_REFUSED      = "ff-refused"         # ─┘
    HEAD_MOVED      = "head-moved"

@dataclass(frozen=True, slots=True)
class Land:
    result: LandResult
    suite:  SuiteResult | None = None   # the run on the PROSPECTIVE MERGE, when it got that far

class MergeQueue:
    def __init__(self, git, runner, integration: str):
        self._merge_lock = asyncio.Lock()   # canonical term: "merge lock"

    async def land(self, wt: Worktree) -> Land:
        async with self._merge_lock:        # held for rebase → suite → ff. nothing else.
            if self._git.head_branch() != self._integration:  return Land(LandResult.HEAD_MOVED)
            if not self._git.rebase(wt, self._integration):   return Land(LandResult.REBASE_CONFLICT)
            suite = await self._runner.run(wt.path)
            if not suite.green:                  return Land(LandResult.SUITE_RED, suite)
            if not self._git.merge_ff_only(wt.branch):  return Land(LandResult.FF_REFUSED, suite)
            return Land(LandResult.LANDED, suite)
```

The suite runs on the *prospective* merge result, so `merge --ff-only` is only ever a
fast-forward of an already-verified tree: the integration branch is correct by construction.
The lock is never held while the Editor reasons, so one sub-issue's integration failure never
stalls the queue for its siblings.

**The queue writes nothing, anywhere.** Its job is to decide whether this tree may become the
integration branch, and to say so. Recording *that* a sub-issue landed is a state transition, and
state transitions belong to the scheduler — two writers for one fact is one writer too many.

`Land` carries the suite out with it because that suite is the **only honest one** for an
`integration-failed` sub-issue. The worktree's own run was green — that is why it reached the queue
at all — and handing the Editor a green `SuiteResult` beside an integration failure would be
handing it a contradiction the harness manufactured for it.

### `Scheduler`

```python
# scheduler.py

class Scheduler:
    async def run(self) -> RunReport:
        await self._refuse_a_red_base()    # red base, no agent starts. fatal.
        graph, states = self._store.read_graph()
        while True:
            for id in sorted(eligible(graph, states)):
                states[id] = SubIssueState.IN_PROGRESS   # claimed BEFORE this coroutine can yield
                running.add(asyncio.create_task(self._pipeline(graph.sub_issues[id], capacity)))
            if not running:
                return RunReport(notify(graph, states, landed, failures))  # ONE notification
            # FIRST_COMPLETED, never gather: eligibility is re-derived on every completion, so a
            # sub-issue starts the moment its blockers land. THERE IS NO WAVE BARRIER.
            done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                ...   # LANDED, or NEEDS_HUMAN + its FailureReport. Never a retry.

    async def _pipeline(self, sub: SubIssue, capacity: asyncio.Semaphore) -> _Closed:
        """One sub-issue's whole life. At most three cycles."""
        async with capacity:                # held for the WHOLE pipeline, landing included: a
            while True:                     # sub-issue is not finished until it is on integration
                closed = await self._cycle(sub)
                if closed is not None:
                    return closed           # None means `revise` — go round again, clean

    async def _cycle(self, sub: SubIssue) -> _Closed | None:
        """One Implementer session, plus the Editor session that follows it if it failed."""
        attempt = self._ledger.spent(sub.id) + 1
        wt = self._git.add_worktree(f"ralph/{sub.id}", ..., self._integration)
        brief, findings = self._store.content(sub.id)   # cycle 2: the REWRITTEN brief. Not a retry.
        t = await self._implementer.run(brief, findings, wt, self._budget)
        suite = await self._runner.run(wt.path)
        outcome = classify_implementer(t, suite)

        if route(Actor.IMPLEMENTER, outcome) is Destination.MERGE_QUEUE:
            land = await self._merge_queue.land(wt)
            if land.result is LandResult.LANDED:
                return self._landed(sub)
            outcome = Outcome.INTEGRATION_FAILED   # worktree preserved for the Editor
            suite = land.suite or suite            # the PROSPECTIVE-MERGE suite, not the
                                                   # green one it had in isolation
        report = failure_report(outcome, t, suite, detail, cycles=attempt)

        if route(Actor.IMPLEMENTER, outcome) is not Destination.EDITOR:
            return await self._quarantine(...)  # ceiling/infra → human. NO CYCLE SPENT.
        if self._editor is None:
            return await self._quarantine(...)  # no Editor in this run: quarantine-and-drain
        return await self._adjudicate(sub, wt, brief, findings, report)

    async def _adjudicate(self, sub, wt, brief, findings, report) -> _Closed | None:
        self._ledger.spend(sub.id)          # spent when the Editor half BEGINS, not when it ends:
                                            # `must_be_terminal` below has to already count this
                                            # cycle, or the third Editor is never told it is the
                                            # last one — and a killed Editor would cost nothing,
                                            # buying its sub-issue infinite Implementers.
        must_be_terminal = self._ledger.must_be_terminal(sub.id)
        t, verdict = await self._editor.adjudicate(
            brief, findings, report, wt, self._budget, must_be_terminal,
        )
        outcome = classify_editor(t, verdict)

        if route(Actor.EDITOR, outcome) is Destination.HUMAN:
            # Escalate on the EDITOR's outcome, not the Implementer's. That the harness cannot
            # adjudicate the failure has displaced the failure as the interesting fact.
            return await self._quarantine(..., failure_report(outcome, t, report.suite, ...))
        if verdict.verdict.is_terminal:      # planning-defect / inconclusive
            return await self._quarantine(..., report)
        if must_be_terminal:
            # THE SCHEDULER REJECTS IT, AND ONLY THE SCHEDULER. It was told this was the final
            # cycle and asked for another anyway. A rule enforced by asking a model nicely is
            # not a rule.
            return await self._quarantine(..., report)

        revised_brief, revised_findings = verdict.revision
        await self._store.record_revision(
            sub.id, revised_brief,
            revised_findings if revised_findings is not None else findings,  # None = leave them
        )
        self._git.discard_worktree(wt)   # the checkout AND the branch. The work is DISCARDED: the
        return None                      # next cycle is cut from integration, not from the wreckage
```

**There is no `exhausted()` pre-check, and there does not need to be.** The third cycle makes
`must_be_terminal` true, and a `revise` returned against it is refused — so the loop cannot reach a
fourth Implementer session by any path. A guard at the top of the loop would be a second enforcement
of the same rule, which is how one rule acquires two homes and the two drift.

The `editor` is `Editor | None`, and the `None` means **there is no Editor in this run** — not a
null Editor. An adapter that always returned no verdict would be a lie the taxonomy would faithfully
propagate: `classify_editor` calls a verdictless Editor `infra-failed`, so every impasse in the run
would reach the human reported as a harness crash. Without an Editor the run is quarantine-and-drain
and failures escalate on the Implementer's own outcome. That is the cheap run, not a degraded one.

When a sub-issue escalates, the run does **not** stop. Everything transitively blocked by it
never becomes eligible and never gets a turn; every unaffected sub-issue continues and lands;
the run ends with **one** notification. A system that pages you the instant the first thing
goes wrong trains you to ignore it.

### `RunLog`

Append-only, one line per event. Two kinds of thing and nothing else: session states, and
Editor verdicts. Token spend, diffstats, and failing-test output belong in the impasse report.

`RunLog` is a **Protocol in `ports.py`**, implemented by `adapters/runlog.py` (JSONL on disk).
It has to be: the merge queue and the scheduler both take one, and orchestration may not name a
concrete adapter — that privilege belongs to `cli.py` alone. It also gives the scheduler's tests
an in-memory log to assert against instead of a temp file.

It is a different sink from `IssueStore.write_event`, and deliberately so. The store mirrors a
transition back into the issue tracker and is **best-effort** — a run must not die because Linear
was unreachable. The run log is the harness's **authoritative** record of what happened, in order.
Same `Event`, two sinks, different durability.

```python
# domain/model/event.py — not runlog.py: `IssueStore.write_event` and `RunLog.write` both take one,
# and `ports.py` may not import orchestration. Both adapters import it from here.

@dataclass(frozen=True, slots=True)
class Event:
    ts: datetime
    sub_issue: SubIssueId
    actor: Actor        # a cycle closes TWO sessions against one sub-issue. Without this, the
                        # authoritative record cannot say whether `session-closed: infra-failed`
                        # means the Implementer crashed or the Editor did.
    kind: Literal["session-opened", "session-closed", "terminal", "verdict"]
    payload: Outcome | Verdict | SubIssueState
```

A cycle's worth of run log, then, reads as a story with two characters in it:

```
01  implementer  session-opened  in-progress
01  implementer  session-closed  impasse
01  editor       session-opened  in-progress
01  editor       session-closed  success        ← the EDITOR's session succeeded…
01  editor       verdict         revise         ← …and this is what it found
01  implementer  session-opened  in-progress    ← cycle two, against a rewritten brief
01  implementer  session-closed  success
01  implementer  terminal        landed
```

### The pre-flight — `domain/rules/preflight.py`, gathered in `cli.py`

**It refuses; it does not warn.** Five checks, and each one describes a repository the harness
would go on to damage or misjudge:

| Check | What it would otherwise do |
|---|---|
| `protected-branch` | Fast-forward `main`. Ralph lands onto the branch it is run from. |
| `dirty-tree` | Fight the merge queue's fast-forwards over uncommitted work, and lose it. |
| `no-suite` | Call every session green. `silent-red` becomes **unreachable** — the most expensive of the five. |
| `hooks-not-installed` | Land commits that skipped the checks the repo believes it enforces. |
| `graph` | Read a graph it cannot read. |

The **rule is pure** — `RepoFacts` in, `tuple[Refusal, ...]` out — so each refusal's sentence can
be tested without a repository to be wrong about. The gathering is `cli.py`'s, because only the
composition root may hold a `GitCli` and an `IssueStore` at once.

Two things it does not do, both on purpose. It does not stop at the first refusal: a human fixing
their morning should learn everything wrong with it in one pass. And it does not *paraphrase* — the
graph's refusal carries `IssueParseError`'s own message verbatim, because "your graph has a cycle"
and "`03-sub.md` has no acceptance criteria" are different mornings, and a pre-flight that flattened
both into "the graph is bad" would have kept the more useful half to itself.

`ralph run` runs the same checks and raises `Refused`. A check that only fires when a human
remembers to ask for it is a check the run does not have.

`ralph run --dry-run` reports the build order, and gets its waves by asking **`eligible`** — the
same rule the scheduler asks — over and over, rather than by a second topological sort. A dry run
whose plan is not the run's plan is worse than no dry run: it is a second opinion about the graph,
free to disagree with the one the scheduler acts on.

---

## 5. Mapping to the build order

| PRD §10 | Module |
|---|---|
| Merge queue | `mergequeue.py` — merge lock, rebase, re-run suite in the worktree, fast-forward |
| Failure taxonomy + base-green | `domain/rules/classify.py`, `domain/rules/routing.py`, `adapters/runlog.py`, `Scheduler._base_green_check` |
| Context ceiling and timeout | `ports.Budget`, `adapters/context.ContextMeter` + per-CLI `ContextSource` |
| Impasse report format | `domain/model/impasse.py`, `domain/model/failure.py` |
| The Editor | `adapters/claude_editor.py`, `adapters/copilot.py`, `domain/rules/cycles.CycleLedger` |
| Linear sync | `adapters/linear.py` behind the existing `IssueStore` protocol |
| Pre-flight + notification | `domain/rules/preflight.py`, `RunReport`, `cli.render` |

## 6. Verified against the CLIs

Both context signals were confirmed by running a real session and reading what came out. Neither
is on stdout; both are on disk, live.

| | Context signal | Source | Baseline context |
|---|---|---|---|
| **Codex** | `info.last_token_usage.input_tokens` | `~/.codex/sessions/…/rollout-*.jsonl` (JSONL) | ~16k |
| **Copilot** | `usage.prompt_tokens` | `--log-dir` log, `--log-level debug` (JSON blocks) | **56.5k**, cut to **8.4k** |

The Copilot baseline is the one to watch: launched the default way, 47% of the smart zone is gone
before the brief is read — MCP servers, skills and tool schemas, loaded into every session. The
harness disables MCP by name and cuts the Editor's tools to four, which brings a one-word exchange
down from 56.5k to 8.4k. Both signals, and both of those figures, come from running the real CLI
and reading what came out.

**Neither CLI's obvious channel is the right one.** Codex's `--json` stream reports `turn.completed`
only at session end, when the ceiling has nothing left to prevent. Copilot's `--output-format json`
stream reports `outputTokens` — the completion count, never the prompt. In both cases the number
the ceiling needs is written to a file on disk, live, and in neither case is it on stdout.
