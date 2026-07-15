"""The seams. Every Protocol here has a fake in `tests/fakes.py`, and the fakes are what the test
suite runs against. A test that needs a real model, a real network, or a real `codex` binary is in
the wrong layer.

The fakes live under `tests/` and not here on purpose: nothing in `ralph/` may import a fake, and
the only way to guarantee that is for `ralph/` not to contain one.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
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


@dataclass(frozen=True, slots=True)
class SessionContext:
    """The shared inputs for one bounded actor session.

    This is the context an actor receives, not the token context measured by `Observation`.
    """

    brief: Brief
    findings: Findings
    worktree: Worktree
    budget: Budget


@runtime_checkable
class Implementer(Protocol):
    async def run(self, context: SessionContext) -> SessionTelemetry: ...


@runtime_checkable
class Editor(Protocol):
    """Bounded exactly like the Implementer: same Budget, same SessionTelemetry. It can come back
    `ceiling-exceeded` too. Read-only — it never writes code."""

    async def adjudicate(
        self,
        context: SessionContext,
        failure: FailureReport,
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


@dataclass(frozen=True, slots=True)
class Observation:
    """One model call, as the CLI recorded it.

    Three numbers arrive together and only one of them is a bound. Keeping them in one value is
    what stops them being confused for each other: every CLI reports context and consumption in
    the *same* event, adjacent, with names that read alike (`last_token_usage` beside
    `total_token_usage`; `prompt_tokens` beside `total_tokens`), and picking the wrong one builds
    a ceiling that fires on a long cheap session and never on a bloated one.
    """

    context_tokens: int
    """What the model was reasoning over on this call. **The ceiling is on this, and only this.**"""

    consumed_tokens: int
    """Cumulative spend. Telemetry only — nothing is gated on it. It climbs forever, so a bound on
    it would be a bound on session *length*, which is what the wall clock is for."""

    rate_limit_used_percent: float | None
    """How much of the account's rate limit is gone. Logged, never gated: free early warning for
    the 429 that would otherwise arrive as an unexplained `infra-failed`."""


@runtime_checkable
class ContextSource(Protocol):
    """Yields an observation per model call, live, while a session runs.

    Neither CLI puts this on stdout; both write it to disk as the session runs, so every
    implementation of this tails a file.

    An `AsyncGenerator` and not merely an `AsyncIterator`, because **closing is part of the
    contract**: a source that tails a file holds a file handle, and the ceiling kill breaks out of
    the loop mid-stream. `run_bounded` closes it; the type is what obliges it to.
    """

    def observations(self) -> AsyncGenerator[Observation, None]: ...
