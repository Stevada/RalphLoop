"""The seams. Every Protocol here has a fake in `tests/fakes.py`, and the fakes are what the test
suite runs against. A test that needs a real model, a real network, or a real `codex` binary is in
the wrong layer.

The fakes live under `tests/` and not here on purpose: nothing in `ralph/` may import a fake, and
the only way to guarantee that is for `ralph/` not to contain one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ralph.harness import (
    Actor,
    EditorVerdict,
    FailureReport,
    Outcome,
    SessionTelemetry,
    SuiteResult,
)
from ralph.issues import Findings, Spec, SubIssueId
from ralph.runlog import Event


@dataclass(frozen=True, slots=True)
class Budget:
    """The wall-clock backstop for one actor session."""

    wall_clock_s: float = 1800.0


@dataclass(frozen=True, slots=True)
class RepoCommands:
    """Already-split commands declared by the target repo."""

    test: tuple[str, ...]
    install: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class Worktree:
    """An isolated checkout. Where a session works, and where the merge gate re-runs the suite."""

    path: Path
    branch: str
    base: str  # the branch this was cut from


@dataclass(frozen=True, slots=True)
class Candidate:
    """What travels: the Implementer holds it, the merge gate admits it, the Integrator
    reconciles it, the Editor reads it, and a human opens what is left of it.

    Frozen, and it stays frozen. Every result a candidate provokes — telemetry, `Land`, a verdict —
    is returned to the scheduler rather than accumulated here; a value that gathered its own outcomes
    would be the one mutable object threading the whole pipeline, and the rules that decide anything
    about it are pure.

    `id` is the whole identity: a sub-issue never has two candidates at once, so there is nothing
    finer to name. Which cycle produced this one is on `FailureReport.cycles`, not here.
    """

    id: SubIssueId
    spec: Spec
    findings: Findings
    worktree: Worktree


@dataclass(frozen=True, slots=True)
class SessionContext:
    """One **Candidate**, bounded for one actor session.

    The budget is session-scoped and the candidate is not, which is the whole reason these are two
    types: the merge gate takes the candidate alone, because it opens no session of its own.
    """

    candidate: Candidate
    budget: Budget


@runtime_checkable
class Implementer(Protocol):
    async def run(self, context: SessionContext) -> SessionTelemetry: ...


@runtime_checkable
class Integrator(Protocol):
    """Dispatched by the merge gate, into a worktree holding a conflict it just created.

    It is never asked whether the work is *right* — that was settled before the sub-issue reached
    the gate. It is asked only to make two correct trees into one, and it commits that the way an
    Implementer commits anything. It gets the spec because knowing what the work was *for* is how
    you choose between two intents; it has no authority to change what the spec asks.
    """

    async def reconcile(self, context: SessionContext) -> SessionTelemetry: ...


@runtime_checkable
class Editor(Protocol):
    """Bounded exactly like the Implementer: same Budget, same SessionTelemetry.

    Read-only — it never writes code.
    """

    async def adjudicate(
        self,
        context: SessionContext,
        failure: FailureReport,
        must_be_terminal: bool,
    ) -> tuple[SessionTelemetry, EditorVerdict | None]: ...


@runtime_checkable
class RunLog(Protocol):
    """The harness's own append-only record of what happened, in order. Write-through, read-once.

    A Protocol rather than a concrete JSONL writer because the merge gate and the scheduler both
    take one, and orchestration may not name a concrete adapter — that privilege belongs to
    `cli.py` alone.
    """

    async def write(self, e: Event) -> None: ...

    def events(self) -> tuple[Event, ...]: ...


HARNESS_LINE = "ralph| "
"""Prefix on every line of a transcript the harness wrote rather than the session.

Here, beside the port, because both ends of the artifact need the same one: each session adapter
opens its transcript with the launch, and whatever implements `Transcripts` closes it with the
footer. Nothing parses it — the port is write-only. It is for the human's eye, so that the two
accounts in one file are never mistaken for each other.
"""


@dataclass(frozen=True, slots=True)
class FinishedSession:
    """One session, everything the harness observed about it, and what it concluded.

    Carried whole rather than as a body plus a filename because a conclusion is only readable
    beside the observations it was drawn from. `success` on its own is a claim; `success` printed
    under `commits: 0` is a claim a human can check.
    """

    sub_issue: SubIssueId
    cycle: int
    actor: Actor
    outcome: Outcome
    telemetry: SessionTelemetry
    budget: Budget
    merge_finished: bool | None = None
    """The merge gate's observation of the worktree — the one input to a classification that is
    not telemetry, and the whole of what `classify_integrator` asks about the work. Absent for the
    two actors nobody asks it about."""


@runtime_checkable
class Transcripts(Protocol):
    """Where a session's whole output is kept, keyed by sub-issue, cycle and actor.

    A separate port from `RunLog` because the two artifacts have different readers: the run log is
    skimmed in order by someone asking what happened, and a transcript is opened once, deliberately,
    by someone who already knows which session they want. Write-only — the harness never reads one
    back, and a port that offered to would be inviting a decision to be made from a model's prose.
    """

    async def write(self, session: FinishedSession) -> None: ...


@runtime_checkable
class TestRunner(Protocol):
    async def run(self, dir: Path) -> SuiteResult: ...


@runtime_checkable
class CommandSource(Protocol):
    def discover(self, repo: Path) -> RepoCommands: ...


@runtime_checkable
class Git(Protocol):
    def add_worktree(self, branch: str, at: Path, base: str) -> Worktree: ...

    def move_worktree(self, wt: Worktree, to: Path) -> Worktree: ...

    def discard_worktree(self, wt: Worktree) -> None:
        """Destroy the checkout **and its branch** — both, always. Whether that loses work is the
        caller's to know: after a landing the commits are already on the integration branch, and
        after a `revise` verdict losing them is the point. Deleting the branch is not tidiness in
        either case — `git worktree add -b` refuses a name that still exists, so a sub-issue whose
        branch outlived its checkout could not be cut again."""

    def merge(self, wt: Worktree, onto: str) -> bool:
        """Merge the integration branch into the worktree. False on conflict, with the conflict
        left in place — it is the input to conflict resolution, and the evidence a human gets if
        that fails."""
        ...

    def merge_finished(self, wt: Worktree) -> bool:
        """Whether the merge left in this worktree has been committed. The harness's own answer to
        "did the Integrator finish?", asked of git rather than of the model."""
        ...

    def merge_ff_only(self, branch: str) -> bool: ...

    def commits_between(self, base: str, branch: str) -> int: ...

    def head_branch(self) -> str: ...
