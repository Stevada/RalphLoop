# Ralph Loop — Harness Architecture

The Python harness. This document specifies the modules, types, and seams; `docs/prd.md`
specifies *why*, and [`UBIQUITOUS_LANGUAGE.md`](../UBIQUITOUS_LANGUAGE.md) is canonical for
every term used here. Identifiers in this codebase use canonical terms only — an "aliases to
avoid" word appearing as a class, function, field, or state name is a defect.

## Why Python

The bash harness is good at what it currently does: worktrees, rebase, fast-forward, lock.
That is git plumbing, and bash is the right language for it. But every remaining item in
PRD §10 — the failure taxonomy, the 120k ceiling, the impasse report, the Editor, Linear
sync — is a thing bash is structurally bad at, and the ceiling in particular requires
consuming a JSONL event stream and killing a process mid-session. The mix has flipped: git
plumbing is now the minority of the code.

One consequence is worth stating up front, because it deletes code. A **run** becomes a
single process: the merge lock is an `asyncio.Lock` rather than `flock`, and the PID
tracking, result files, and `reap_finished` polling in `parallel-codex.sh` all go away.

## Layers

The dependency arrow points inward, always.

```
ralph/
  domain/        pure. no I/O. stdlib only.
  ports.py       Protocols — the seams. every one has a fake.
  adapters/      codex, copilot, claude_editor, context, git, filesystem, linear, test runner
  mergequeue.py  \
  scheduler.py    } orchestration
  runlog.py      /
  cli.py         composition root — the only place a concrete adapter is named
```

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
# domain/graph.py

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
# domain/content.py

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
# domain/outcomes.py

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
    final_test_output: str
    impasse_report: ImpasseReport | None     # parsed from the <impasse> sentinel

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
# domain/classify.py

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

### Routing

The taxonomy table, executable, one test per row. `ceiling-exceeded` and `infra-failed` route
identically for both actors; only `SUCCESS` needs to know who is asking, because an
Implementer's success goes to the merge queue and an Editor's success is a verdict to act on.

```python
# domain/routing.py

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
# domain/verdicts.py

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
# domain/lifecycle.py

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
    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]: ...
    def content(self, id: SubIssueId) -> tuple[Brief, Findings]: ...
    async def record_revision(self, id, brief: Brief, findings: Findings) -> None: ...
    async def write_event(self, e: Event) -> None: ...    # write-through, best-effort

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

class ContextSource(Protocol):
    """Yields the context size on each model call, live, while a session runs."""
    def observations(self) -> AsyncIterator[int]: ...
```

```python
# adapters/context.py

class ContextMeter:
    """Watches context, not consumption. Records the high-water mark for telemetry and
    trips when the smart zone is left. Shared by every adapter."""
    def observe(self, context_tokens: int) -> None: ...
    @property
    def peak(self) -> int: ...
    @property
    def exceeded(self) -> bool: ...

async def run_bounded(proc, source: ContextSource, budget: Budget) -> tuple[str | None, int]:
    """The kill loop. Every adapter's `run`/`adjudicate` is this plus prompt rendering."""
    meter, killed = ContextMeter(budget.max_context_tokens), None
    try:
        async with asyncio.timeout(budget.wall_clock_s):
            async for ctx in source.observations():
                meter.observe(ctx)
                if meter.exceeded:
                    proc.kill(); killed = "ceiling"; break
    except TimeoutError:
        proc.kill(); killed = "wall-clock"          # the stuck-session catcher
    return killed, meter.peak
```

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
identifies which rollout file belongs to this session.

The ceiling (120k) sits far below the window (272k), so it always fires **before** Codex would
auto-compact — compaction never gets the chance to drop the context back under the bound and
hide the crossing. `rate_limits.primary.used_percent` rides along in the same event: free early
warning for the 429 → `infra-failed` case. Log it; don't gate on it yet.

### `CopilotImplementer` — `adapters/copilot.py`

`copilot -p <prompt> --allow-all-tools --model <m> --log-dir <wt> --log-level debug`.

Copilot has no `--json` event stream, but its **debug log** carries the same signal Codex's
rollout file does — one `usage` block per model call. Verified against the CLI:

```json
"usage": {
  "prompt_tokens": 56743,        ← the context on this call. THE CEILING.
  "total_tokens":  56746,        ← consumption. telemetry only.
  "prompt_tokens_details": { "cached_tokens": 56577 }
}
```

`CopilotContextSource` tails the log and yields `usage.prompt_tokens`. Three things differ
from Codex and all three are load-bearing:

- **`--log-level debug` is required.** At the default level the `usage` blocks are absent and
  the ceiling silently stops working. The adapter must set it, not assume it.
- **The log is pretty-printed JSON inside a text log, not JSONL.** Parsing needs a streaming
  brace-matched block extractor, not `json.loads` per line.
- **Copilot starts at ~56k of context**, before the brief is even read — MCP servers and their
  tool schemas are loaded into every session. That is **47% of the 120k smart zone consumed at
  turn zero**. The harness must run Copilot with MCP servers disabled, or the ceiling will fire
  on work that never had room to begin with. Codex, by comparison, started at ~16k.

`--max-ai-credits` is a *credit* cap, not a context cap. It is not a substitute for the ceiling.

### `ClaudeCodeEditor` — `adapters/claude_editor.py`

The Editor writes only the brief and findings. It may read anything and run read-only commands.
It never commits, never cherry-picks, never touches a worktree except to read it — the moment it
commits it is an Implementer with a different name. **That is enforced by the tool allowlist,
not by the prompt.**

```python
class ClaudeCodeEditor:
    READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash"]

    async def adjudicate(self, brief, findings, failure, worktree, budget, must_be_terminal):
        options = ClaudeAgentOptions(
            model="claude-opus-4-8",
            allowed_tools=self.READ_ONLY_TOOLS,
            can_use_tool=self._refuse_mutating_bash,   # git commit / cherry-pick / write → deny
            cwd=worktree.path,
        )
        ...   # rejects REVISE when must_be_terminal
```

The SDK streams `ResultMessage.usage`, so its `ContextSource` needs no log tailing.

### `CopilotEditor` — `adapters/copilot.py`

Same contract, enforced by the CLI's own flags rather than an SDK callback:

```python
class CopilotEditor:
    DENY = ["write", "edit", "shell(git commit)", "shell(git cherry-pick)", ...]
    # copilot -p <prompt> --deny-tool ... --allow-tool 'shell(git diff)' ...
```

`--deny-tool` / `--allow-tool` are the enforcement surface. They are coarser than the SDK's
`can_use_tool` callback, so this adapter's read-only guarantee is **weaker** — worth knowing
when choosing which Editor to run. Same ceiling caveat as `CopilotImplementer`.

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

class MergeQueue:
    def __init__(self, git, runner, integration: str, log: RunLog):
        self._merge_lock = asyncio.Lock()   # canonical term: "merge lock"

    async def land(self, sub: SubIssue, wt: Worktree) -> LandResult:
        async with self._merge_lock:        # held for rebase → suite → ff. nothing else.
            if self._git.head_branch() != self._integration:  return LandResult.HEAD_MOVED
            if not self._git.rebase(wt, self._integration):   return LandResult.REBASE_CONFLICT
            if not (await self._runner.run(wt.path)).green:   return LandResult.SUITE_RED
            if not self._git.merge_ff_only(wt.branch):        return LandResult.FF_REFUSED
            await self._log.landed(sub.id)  # merge first, then write
            return LandResult.LANDED
```

The suite runs on the *prospective* merge result, so `merge --ff-only` is only ever a
fast-forward of an already-verified tree: the integration branch is correct by construction.
The lock is never held while the Editor reasons, so one sub-issue's integration failure never
stalls the queue for its siblings.

### `Scheduler`

```python
# scheduler.py

class Scheduler:
    async def run(self) -> RunReport:
        await self._base_green_check()     # red base, no agent starts. fatal.
        await self._install_deps_once()    # in the base checkout. never `|| true`.
        async with asyncio.TaskGroup() as tg:
            ...                            # dispatch on eligibility, capped by a semaphore
        return self._report()              # one notification, at the end

    async def _pipeline(self, sub: SubIssue) -> None:
        """One sub-issue's whole life. At most three cycles."""
        while True:
            wt = self._git.add_worktree(f"ralph/{sub.id}", ..., self._integration)
            brief, findings = self._store.content(sub.id)
            t = await self._implementer.run(brief, findings, wt, self._budget)
            outcome = classify_implementer(t, await self._runner.run(wt.path))

            if outcome is Outcome.SUCCESS:
                result = await self._merge_queue.land(sub, wt)
                if result is LandResult.LANDED:
                    return self._mark(sub, SubIssueState.LANDED)
                outcome = Outcome.INTEGRATION_FAILED   # worktree preserved for the Editor

            match route(Actor.IMPLEMENTER, outcome):
                case Destination.HUMAN:       # ceiling-exceeded or infra-failed. no cycle spent.
                    return self._mark(sub, SubIssueState.NEEDS_HUMAN)
                case Destination.EDITOR:
                    pass

            if self._ledger.exhausted(sub.id):
                return self._mark(sub, SubIssueState.NEEDS_HUMAN)

            t, verdict = await self._editor.adjudicate(
                brief, findings, self._failure_report(sub, t, outcome), wt,
                self._budget, self._ledger.must_be_terminal(sub.id),
            )
            self._ledger.spend(sub.id)

            if route(Actor.EDITOR, classify_editor(t, verdict)) is Destination.HUMAN:
                return self._mark(sub, SubIssueState.NEEDS_HUMAN)
            if verdict.verdict.is_terminal:
                return self._mark(sub, SubIssueState.NEEDS_HUMAN)

            await self._store.record_revision(sub.id, verdict.revised_brief,
                                              verdict.revised_findings)
            # On Editor entry the Implementer's work is discarded. It restarts clean
            # against the revised brief. Knowledge survives only in the findings.
```

When a sub-issue escalates, the run does **not** stop. Everything transitively blocked by it
never becomes eligible and never gets a turn; every unaffected sub-issue continues and lands;
the run ends with **one** notification. A system that pages you the instant the first thing
goes wrong trains you to ignore it.

### `RunLog`

Append-only, one line per event. Two kinds of thing and nothing else: session states, and
Editor verdicts. Token spend, diffstats, and failing-test output belong in the impasse report.

```python
# runlog.py

@dataclass(frozen=True, slots=True)
class Event:
    ts: datetime
    sub_issue: SubIssueId
    kind: Literal["session-opened", "session-closed", "terminal", "verdict"]
    payload: Outcome | Verdict | SubIssueState
```

---

## 5. Mapping to the build order

| PRD §10 | Module |
|---|---|
| Merge queue | `mergequeue.py` — merge lock, rebase, re-run suite in the worktree, fast-forward |
| Failure taxonomy + base-green | `domain/classify.py`, `domain/routing.py`, `runlog.py`, `Scheduler._base_green_check` |
| Context ceiling and timeout | `ports.Budget`, `adapters/context.ContextMeter` + per-CLI `ContextSource` |
| Impasse report format | `domain/impasse.py` |
| The Editor | `adapters/claude_editor.py`, `adapters/copilot.py`, `domain/verdicts.CycleLedger` |
| Linear sync | `adapters/linear.py` behind the existing `IssueStore` protocol |
| PR + CI + notification | `RunReport`, `ralph validate` refusing repos without PR CI |

## 6. Verified against the CLIs

Both context signals were confirmed by running a real session and reading what came out. Neither
is on stdout; both are on disk, live.

| | Context signal | Source | Baseline context |
|---|---|---|---|
| **Codex** | `info.last_token_usage.input_tokens` | `~/.codex/sessions/…/rollout-*.jsonl` (JSONL) | ~16k |
| **Copilot** | `usage.prompt_tokens` | `--log-dir` log, `--log-level debug` (JSON blocks) | **~56k** |

The Copilot baseline is the one to watch: 47% of the smart zone is gone before the brief is read,
because MCP servers load their tool schemas into every session. Disable them for harness runs.
