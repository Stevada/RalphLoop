"""The Claude SDK session's failure boundary.

Everything else about this adapter is tested through the `OpenSession` seam with a stub, so no test
calls the API. What a stub cannot stand in for is the adapter's *own* collapse — the SDK absent, the
SDK broken — because the stub is the thing that would have collapsed. That is what is tested here,
and it is tested by making the import fail on any machine, installed or not.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ralph.adapters.claude.session import claude_sdk_session
from ralph.adapters.runtime.turn_stream import Permission, TurnStreamAsk, run_turn_stream
from ralph.ports import Budget

GENEROUS_CLOCK = Budget(wall_clock_s=30.0)


def _ask(cwd: Path) -> TurnStreamAsk:
    return TurnStreamAsk(prompt="adjudicate", cwd=cwd, permit=lambda tool, input: Permission(True))


async def test_a_conversation_that_cannot_start_ends_the_stream_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failure this file exists for.

    `claude-agent-sdk` is an optional extra, so a run configured for a Claude Editor on a machine
    that never installed it reaches this adapter and dies before saying anything. The session must
    still *end*: the reader is waiting on a queue only this coroutine can close, and an Editor
    session that never returns holds its sub-issue open for the life of the run.
    """
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    session = claude_sdk_session(_ask(tmp_path))
    completed = await asyncio.wait_for(run_turn_stream(session, GENEROUS_CLOCK), timeout=10)

    assert completed.bound.killed is None  # it ended on its own, nowhere near the wall clock
    assert completed.exit_code != 0
    # And it said why, in the transcript — the only place a human reads an Editor's account of
    # itself. A verdict is absent, so the scheduler will call this `infra-failed` and page them.
    assert "claude_agent_sdk" in completed.output


async def test_killing_a_dead_conversation_still_ends_its_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`TurnStreamSession` requires that `kill()` *end* `turns()`. Cancelling a task that already
    finished does nothing at all, so a `kill()` that only cancelled would leave the reader waiting —
    and the wall clock, having already fired, would have nothing left to try."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    session = claude_sdk_session(_ask(tmp_path))
    await session.wait()

    async def drain() -> list[str | object]:
        return [turn async for turn in session.turns()]

    await asyncio.wait_for(drain(), timeout=5)  # the sentinel the crashed conversation queued
    session.kill()

    assert await asyncio.wait_for(drain(), timeout=5) == []  # and one from `kill()`, on an empty queue
