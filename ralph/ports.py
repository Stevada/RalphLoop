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

from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry, SuiteResult
from ralph.issues import Findings, Spec
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
    """An isolated checkout. Where a session works, and where the merge queue re-runs the suite."""

    path: Path
    branch: str
    base: str  # the branch this was cut from


@dataclass(frozen=True, slots=True)
class SessionContext:
    """The shared inputs for one bounded actor session."""

    spec: Spec
    findings: Findings
    worktree: Worktree
    budget: Budget


@runtime_checkable
class Implementer(Protocol):
    async def run(self, context: SessionContext) -> SessionTelemetry: ...


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
class CommandSource(Protocol):
    def discover(self, repo: Path) -> RepoCommands: ...


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
