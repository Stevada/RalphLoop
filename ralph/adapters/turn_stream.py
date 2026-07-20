"""The SDK-backed session seam — the one transport every in-process actor shares.

An SDK actor session is the same event whatever role it plays: a bounded stream of turns, each turn
either something the model said or what a model call cost. `ClaudeCodeEditor`, `CopilotEditor`, and
the `CopilotImplementer` all run on this seam; what differs is only the prompt, the permit, and how
the emitted output is read afterwards.

This is the SDK twin of the subprocess `Session` in `session.py`: two transports behind one pair of
role cores, wired to concrete agents only in `cli.py`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ralph.adapters.context import Bound, run_bounded
from ralph.ports import Budget


@dataclass(frozen=True, slots=True)
class Permission:
    """Allowed, or denied and why. The `why` goes to the model, so it can try something legal
    rather than concluding the repository is broken."""

    allowed: bool
    reason: str = ""


Permit = Callable[[str, dict[str, object]], Permission]


@dataclass(frozen=True, slots=True)
class TurnStreamAsk:
    """Everything an SDK-backed actor session needs to start."""

    prompt: str
    cwd: Path
    permit: Permit | None = None


OpenSession = Callable[[TurnStreamAsk], "TurnStreamSession"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token consumption reported by a model call."""

    consumed_tokens: int


Turn = str | TokenUsage
"""What a session emits as it goes: something it said, or usage from a model call."""


@runtime_checkable
class TurnStreamSession(Protocol):
    """A running SDK-backed actor conversation. The bounding does not care which actor owns it.

    `kill()` must make `turns()` **end**, not raise: it is how the wall clock stops a session, and
    a kill that surfaced as a `CancelledError` three layers up would be reported as `infra-failed`
    for the wrong reason.
    """

    @property
    def returncode(self) -> int | None: ...

    def turns(self) -> AsyncGenerator[Turn, None]: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


@dataclass(frozen=True, slots=True)
class TurnStreamRun:
    """A bounded SDK turn stream, before an actor-specific adapter interprets its output."""

    bound: Bound
    exit_code: int
    output: str
    wall_clock_s: float


async def run_turn_stream(session: TurnStreamSession, budget: Budget) -> TurnStreamRun:
    """Run an SDK turn stream under the wall-clock bound and collect what it emitted."""
    started = time.monotonic()
    said: list[str] = []
    consumed_tokens = 0

    async def pump() -> None:
        nonlocal consumed_tokens
        async for turn in session.turns():
            if isinstance(turn, TokenUsage):
                consumed_tokens = max(consumed_tokens, turn.consumed_tokens)
            else:
                said.append(turn)

    reading = asyncio.create_task(pump())
    bound = await run_bounded(_TurnStreamKillable(session, reading), budget)
    await reading

    return TurnStreamRun(
        bound=Bound(killed=bound.killed, consumed_tokens=consumed_tokens),
        exit_code=session.returncode if session.returncode is not None else -1,
        output="".join(said),
        wall_clock_s=time.monotonic() - started,
    )


@dataclass(frozen=True, slots=True)
class _TurnStreamKillable:
    """Clock-bound the turn stream, because an in-process actor may not have subprocess wait
    semantics. The session is finished when its turns are drained."""

    session: TurnStreamSession
    reading: asyncio.Task[None]

    @property
    def returncode(self) -> int | None:
        return self.session.returncode

    def kill(self) -> None:
        self.session.kill()

    async def wait(self) -> int:
        await asyncio.shield(self.reading)
        return await self.session.wait()
