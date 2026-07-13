"""The seams. Every Protocol here has a fake in `tests/fakes.py`, and the fakes are what the test
suite runs against. A test that needs a real model, a real network, or a real `codex` binary is in
the wrong layer.

The fakes live under `tests/` and not here on purpose: nothing in `ralph/` may import a fake, and
the only way to guarantee that is for `ralph/` not to contain one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ralph.domain import (
    Brief,
    EditorVerdict,
    Event,
    FailureReport,
    Findings,
    IssueGraph,
    SessionTelemetry,
    SubIssueId,
    SubIssueState,
    SuiteResult,
)


@dataclass(frozen=True, slots=True)
class Budget:
    """Two bounds that catch different failures; neither substitutes for the other.

    max_context_tokens — the smart zone. A quality bound, not a cost cap: a model reasoning over
    200k of context is a worse engineer than the same model over 100k. One number for both actors,
    regardless of which model runs. The ceiling is on context, not on consumption.

    wall_clock_s — the backstop, and the thing that catches a *stuck* session. A session re-running
    a failing suite twenty times has a flat context and will never trip the ceiling; only the clock
    stops it.
    """

    max_context_tokens: int = 120_000
    wall_clock_s: float = 1800.0


@dataclass(frozen=True, slots=True)
class Worktree:
    """An isolated checkout. Where a session works, and where the merge queue re-runs the suite."""

    path: Path
    branch: str
    base: str  # the branch this was cut from


@runtime_checkable
class Implementer(Protocol):
    async def run(
        self, brief: Brief, findings: Findings, worktree: Worktree, budget: Budget
    ) -> SessionTelemetry: ...


@runtime_checkable
class Editor(Protocol):
    """Bounded exactly like the Implementer: same Budget, same SessionTelemetry. It can come back
    `ceiling-exceeded` too. Read-only — it never writes code."""

    async def adjudicate(
        self,
        brief: Brief,
        findings: Findings,
        failure: FailureReport,
        worktree: Worktree,
        budget: Budget,
        must_be_terminal: bool,
    ) -> tuple[SessionTelemetry, EditorVerdict | None]: ...


@runtime_checkable
class IssueStore(Protocol):
    """The issue tracker: the sub-issue files today, Linear later.

    `write_event` mirrors a transition back into the tracker and is **best-effort** — the tracker
    is a convenience for humans, and a run must not die because it was unreachable. The harness's
    own authoritative record is the `RunLog`, which is a different sink with different durability.
    """

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]: ...

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]: ...

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None: ...

    async def write_event(self, e: Event) -> None: ...


@runtime_checkable
class RunLog(Protocol):
    """The harness's own append-only record of what happened, in order. Write-through, read-once.

    A Protocol rather than a concrete JSONL writer because the merge queue and the scheduler both
    take one, and orchestration may not name a concrete adapter — that privilege belongs to
    `cli.py` alone.
    """

    async def write(self, e: Event) -> None: ...

    def events(self) -> tuple[Event, ...]: ...


@runtime_checkable
class TestRunner(Protocol):
    async def run(self, dir: Path) -> SuiteResult: ...


@runtime_checkable
class Git(Protocol):
    def add_worktree(self, branch: str, at: Path, base: str) -> Worktree: ...

    def move_worktree(self, wt: Worktree, to: Path) -> Worktree: ...

    def discard_worktree(self, wt: Worktree) -> None:
        """Destroy the checkout **and its branch**. What "the Implementer's work is discarded"
        means in git: the next cycle re-cuts the same branch name from the integration branch, and
        it must be cut from the integration branch — not from the wreckage of the last attempt."""

    def rebase(self, wt: Worktree, onto: str) -> bool: ...

    def merge_ff_only(self, branch: str) -> bool: ...

    def commits_between(self, base: str, branch: str) -> int: ...

    def head_branch(self) -> str: ...


@runtime_checkable
class ContextSource(Protocol):
    """Yields the context size on each model call, live, while a session runs.

    Neither CLI puts this on stdout; both write it to disk as the session runs, so every
    implementation of this tails a file.
    """

    def observations(self) -> AsyncIterator[int]: ...
