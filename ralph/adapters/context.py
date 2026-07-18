"""Bounding a running model session.

The wall clock is the only live bound. Context ceiling symbols remain for the migration phase, but
`run_bounded` no longer watches a context source and never kills a session for context.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ralph.harness import Killed
from ralph.ports import Budget, ContextSource, Observation

@dataclass(slots=True)
class ContextMeter:
    """Watches context, records everything, gates on one thing.

    The high-water mark is kept **whether or not it tripped** — a session that peaked at 118k did
    not fail, and the fact that it nearly did is the most useful number in its telemetry.
    """

    ceiling: int
    peak: int = 0
    consumed: int = 0
    rate_limit_used_percent: float | None = None

    def observe(self, o: Observation) -> None:
        self.peak = max(self.peak, o.context_tokens)
        self.consumed = max(self.consumed, o.consumed_tokens)
        if o.rate_limit_used_percent is not None:
            self.rate_limit_used_percent = o.rate_limit_used_percent

    @property
    def exceeded(self) -> bool:
        """True once the session has left the smart zone. Reads `peak`, not the last observation:
        a model that touched 130k and then compacted back to 90k has already done its bad
        thinking, and the compaction must not be allowed to hide the crossing."""
        return self.peak >= self.ceiling


@dataclass(frozen=True, slots=True)
class Bound:
    """What bounding a session learned about it, whether or not it had to intervene."""

    killed: Killed | None
    peak_context_tokens: int
    consumed_tokens: int


@runtime_checkable
class Killable(Protocol):
    """A session the harness can stop. An `asyncio.subprocess.Process` is one; so is the Editor's
    in-process SDK conversation, which is not a subprocess at all.

    Not in `ports.py`, because it is not a seam of the *harness* — nothing above the adapters knows
    a session can be killed. It is the shape `run_bounded` needs, and it lives where `run_bounded`
    does. Two implementations, so it is a real seam and not a hypothetical one.
    """

    @property
    def returncode(self) -> int | None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


async def run_bounded(proc: Killable, source: ContextSource | None, budget: Budget) -> Bound:
    """Run a process to completion under the wall-clock bound.

    `source` is accepted only to keep the interim API stable while the ceiling vocabulary is retired.
    It is deliberately not consumed: peak context is zero, and a context crossing cannot kill.
    """
    del source
    killed: Killed | None = None

    try:
        async with asyncio.timeout(budget.wall_clock_s):
            await proc.wait()
    except TimeoutError:
        killed = "wall-clock"

    if killed is not None and proc.returncode is None:
        proc.kill()
    await proc.wait()

    return Bound(killed=killed, peak_context_tokens=0, consumed_tokens=0)


async def tail(path: Path, until: asyncio.Event, poll_s: float) -> AsyncGenerator[str, None]:
    """Yield the file's lines as they are appended, until `until` is set and the file is exhausted.

    The stopping condition is **a read that came back empty**, and only then whether the session is
    over — in that order, so the tail can never stop with unread lines still behind it. The last
    model call of a session is written a moment before the process exits, and it is the one nearest
    the ceiling: the only one that could still have tripped it.
    """
    buffer = ""
    with path.open() as f:
        while True:
            chunk = f.read()
            if chunk:
                buffer += chunk
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    yield line
                continue
            if until.is_set():
                return
            await asyncio.sleep(poll_s)
