"""The smart-zone ceiling. Written once, here, and shared by every adapter that runs a model.

**The ceiling is on context, not on consumption.** 120,000 tokens is the size of the model's
*smart zone* — the region where its judgment is reliable. It is not a budget, not a share of the
window, and not a count of what the session spent. A model reasoning over 200k of context is a
worse engineer than the same model over 100k, and it is a worse engineer *cheaply*. Cost is not
the argument; quality is.

The two bounds catch different failures and neither substitutes for the other:

- a session that leaves the smart zone is still working, and its work is getting worse — only the
  **ceiling** stops it;
- a session spinning on a failing suite has a **flat** context and will never trip the ceiling —
  only the **wall clock** stops it.

What differs between CLIs is only where the number is published, so that — and nothing else —
lives behind `ContextSource`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ralph.domain import Killed
from ralph.ports import Budget, ContextSource, Observation

log = logging.getLogger(__name__)


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
    """Run a process to completion under both bounds. Every adapter's `run` is this plus a prompt.

    `source=None` means *this session publishes no context signal* — the stand-in agent, or any
    plain `RALPH_AGENT_CMD`. It is bounded on the clock alone, and its peak is honestly reported
    as zero rather than invented.
    """
    meter = ContextMeter(ceiling=budget.max_context_tokens)
    killed: Killed | None = None

    try:
        async with asyncio.timeout(budget.wall_clock_s):
            if source is not None:
                async with aclosing(source.observations()) as observations:
                    async for o in observations:
                        meter.observe(o)
                        if meter.exceeded:
                            killed = "ceiling"
                            break
            if killed is None:
                # The source runs dry when the session stops talking, which is not quite the same
                # moment as the session stopping. Wait for the process itself.
                await proc.wait()
    except TimeoutError:
        killed = "wall-clock"

    if killed is not None and proc.returncode is None:
        proc.kill()
    await proc.wait()

    if meter.rate_limit_used_percent is not None:
        log.info("rate limit: %.1f%% used", meter.rate_limit_used_percent)

    return Bound(killed=killed, peak_context_tokens=meter.peak, consumed_tokens=meter.consumed)


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
