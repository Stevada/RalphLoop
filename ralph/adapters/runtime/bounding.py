"""Bounding a running model session.

The wall clock is the only bound.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ralph.harness import Killed
from ralph.ports import Budget


@dataclass(frozen=True, slots=True)
class Bound:
    """What bounding a session learned about it, whether or not it had to intervene."""

    killed: Killed | None
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


async def run_bounded(proc: Killable, budget: Budget) -> Bound:
    """Run a process to completion under the wall-clock bound."""
    killed: Killed | None = None

    try:
        async with asyncio.timeout(budget.wall_clock_s):
            await proc.wait()
    except TimeoutError:
        killed = "wall-clock"

    if killed is not None and proc.returncode is None:
        proc.kill()
    await proc.wait()

    return Bound(killed=killed, consumed_tokens=0)
