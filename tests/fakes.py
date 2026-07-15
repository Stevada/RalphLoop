"""A fake for every Protocol in `ralph.ports`. These are what the test suite runs against.

They are adapters: each one satisfies an interface at a seam. They are scriptable, not clever —
you tell a fake what to return, then assert on what it was asked. None talks to a model, a
network, or a subprocess, which is the whole point.

They live here rather than in `ralph/` because nothing in the shipped package may import a fake,
and the surest way to guarantee that is for the package not to contain one.

`Git` and `TestRunner` also have *real* adapters, tested against real temporary repositories — a
fake git that always says "rebase succeeded" tests nothing. The fakes here are for the layers
above, which have no business knowing what git is.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ralph.harness import (
    EditorVerdict,
    FailureReport,
    SessionTelemetry,
    SuiteResult,
)
from ralph.issues import Brief, Findings, IssueGraph, SubIssueId, SubIssueState
from ralph.ports import Observation, SessionContext, Worktree
from ralph.runlog import Event
from tests.builders import telemetry


@dataclass(slots=True)
class FakeImplementer:
    """Returns scripted telemetry, one per call, and records what it was asked to build."""

    scripted: Sequence[SessionTelemetry] = field(default_factory=list)
    calls: list[SessionContext] = field(default_factory=list)

    async def run(self, context: SessionContext) -> SessionTelemetry:
        self.calls.append(context)
        if not self.scripted:
            return telemetry()
        return self.scripted[min(len(self.calls) - 1, len(self.scripted) - 1)]


@dataclass(slots=True)
class FakeEditor:
    """Returns scripted verdicts, and records everything it was handed.

    `must_be_terminal` is recorded so a test can prove the harness *told* the Editor it was the
    final cycle rather than trusting it to remember — and, separately, that the harness refuses a
    `revise` from an Editor that was told and asked anyway.

    The last scripted entry repeats, so `[revise]` means "always revise" — which is how the cycle
    cap gets an adversary rather than a collaborator.
    """

    scripted: Sequence[tuple[SessionTelemetry, EditorVerdict | None]] = field(default_factory=list)
    calls: list[tuple[SessionContext, FailureReport, bool]] = field(default_factory=list)

    async def adjudicate(
        self,
        context: SessionContext,
        failure: FailureReport,
        must_be_terminal: bool,
    ) -> tuple[SessionTelemetry, EditorVerdict | None]:
        self.calls.append((context, failure, must_be_terminal))
        if not self.scripted:
            return telemetry(commits=0), None
        return self.scripted[min(len(self.calls) - 1, len(self.scripted) - 1)]


@dataclass(slots=True)
class FakeIssueStore:
    """In-memory graph and content. Records every event mirrored into it, in order."""

    graph: IssueGraph
    states: dict[SubIssueId, SubIssueState] = field(default_factory=dict)
    contents: dict[SubIssueId, tuple[Brief, Findings]] = field(default_factory=dict)
    mirrored: list[Event] = field(default_factory=list)
    revisions: list[tuple[SubIssueId, Brief, Findings]] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]:
        return self.graph, dict(self.states)

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]:
        return self.contents.get(id, (Brief(body=f"build {id}"), Findings(body="")))

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None:
        self.contents[id] = (brief, findings)
        self.revisions.append((id, brief, findings))

    async def write_event(self, e: Event) -> None:
        self.mirrored.append(e)

    async def publish_notification(self, body: str) -> None:
        self.notifications.append(body)


@dataclass(slots=True)
class FakeRunLog:
    """In-memory run log. The assertion surface for "the log tells the true story, in order"."""

    written: list[Event] = field(default_factory=list)

    async def write(self, e: Event) -> None:
        self.written.append(e)

    def events(self) -> tuple[Event, ...]:
        return tuple(self.written)


@dataclass(slots=True)
class FakeTestRunner:
    """Green by default. `red_in` names the worktrees whose suite fails — which is how the
    semantic-conflict case gets set up without a real repository."""

    green: bool = True
    red_in: set[Path] = field(default_factory=set)
    output: str = "1 passed"
    runs: list[Path] = field(default_factory=list)

    async def run(self, dir: Path) -> SuiteResult:
        self.runs.append(dir)
        green = self.green and dir not in self.red_in
        return SuiteResult(green=green, output=self.output if green else "1 failed", duration_s=0.0)


@dataclass(slots=True)
class FakeGit:
    """Scriptable git, for the layers that must not know what git is. Real git has a real adapter
    tested against real repositories."""

    head: str = "integration"
    rebase_conflicts: set[str] = field(default_factory=set)  # branches whose rebase fails
    ff_refuses: set[str] = field(default_factory=set)  # branches whose fast-forward is refused
    commit_counts: dict[str, int] = field(default_factory=dict)
    worktrees: list[Worktree] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    moved: list[tuple[str, Path]] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)

    def add_worktree(self, branch: str, at: Path, base: str) -> Worktree:
        wt = Worktree(path=at, branch=branch, base=base)
        self.worktrees.append(wt)
        return wt

    def move_worktree(self, wt: Worktree, to: Path) -> Worktree:
        self.moved.append((wt.branch, to))
        return Worktree(path=to, branch=wt.branch, base=wt.base)

    def discard_worktree(self, wt: Worktree) -> None:
        self.discarded.append(wt.branch)

    def rebase(self, wt: Worktree, onto: str) -> bool:
        return wt.branch not in self.rebase_conflicts

    def merge_ff_only(self, branch: str) -> bool:
        if branch in self.ff_refuses:
            return False
        self.merged.append(branch)
        return True

    def commits_between(self, base: str, branch: str) -> int:
        return self.commit_counts.get(branch, 1)

    def head_branch(self) -> str:
        return self.head


@dataclass(slots=True)
class FakeContextSource:
    """Replays a canned sequence of observations, as if tailing a rollout file."""

    scripted: Iterable[Observation] = field(default_factory=list)

    async def observations(self) -> AsyncGenerator[Observation, None]:
        for o in self.scripted:
            yield o
