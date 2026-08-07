"""The wall-clock bound for running sessions."""

from __future__ import annotations

import asyncio
import sys

import pytest

from ralph.adapters.runtime.bounding import Bound, run_bounded
from ralph.adapters.git import GitCli
from ralph.adapters.runtime.implementer import SubprocessImplementer
from ralph.harness import NOTHING, Outcome, classify_implementer
from ralph.issues import Findings, Spec, SubIssueId
from ralph.ports import Budget, Candidate, SessionContext
from tests.testbed import TargetRepo

SHORT_CLOCK = Budget(wall_clock_s=0.3)
GENEROUS_CLOCK = Budget(wall_clock_s=10.0)


async def spawn(*python: str) -> asyncio.subprocess.Process:
    """A real process to bound."""
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", "".join(python), stdout=asyncio.subprocess.DEVNULL
    )


FOREVER = "import time\nwhile True: time.sleep(0.05)\n"
AT_ONCE = "pass\n"


async def test_a_session_that_finishes_before_the_clock_runs_to_completion() -> None:
    proc = await spawn(AT_ONCE)

    bound = await run_bounded(proc, GENEROUS_CLOCK)

    assert bound == Bound(killed=None, consumption=NOTHING)
    assert proc.returncode == 0


async def test_the_clock_catches_a_stuck_session() -> None:
    proc = await spawn(FOREVER)

    bound = await run_bounded(proc, SHORT_CLOCK)

    assert bound == Bound(killed="wall-clock", consumption=NOTHING)
    assert proc.returncode is not None


class _UnkillableSession:
    """A session whose `kill()` does not work. Not a hypothetical: a `TurnStreamSession` whose
    conversation crashed before it could queue its end-of-stream sentinel is exactly this — the
    reader it was supposed to release is still waiting, and nothing will release it."""

    def __init__(self) -> None:
        self.killed = False

    @property
    def returncode(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


async def test_a_kill_that_does_not_take_still_returns_to_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound's own escape hatch. Reaping a killed session is the last wait in `run_bounded`, and
    an unbounded one there would hang the run *past* the clock that had just fired — leaving a dead
    sub-issue holding a slot for the life of the run, which is what a wall clock exists to prevent.
    """
    monkeypatch.setattr("ralph.adapters.runtime.bounding.KILL_GRACE_S", 0.2)
    session = _UnkillableSession()

    bound = await asyncio.wait_for(run_bounded(session, SHORT_CLOCK), timeout=10)

    assert bound == Bound(killed="wall-clock", consumption=NOTHING)
    assert session.killed  # it was asked to stop, and the harness did not wait forever to be obeyed


async def test_real_session_telemetry_has_no_context_peak(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    implementer = SubprocessImplementer(
        build_argv=lambda spec, findings, worktree: (sys.executable, "-c", AT_ONCE),
    )

    t = await implementer.run(
        SessionContext(
            candidate=Candidate(
                id=SubIssueId("01"),
                spec=Spec(body="build it"),
                findings=Findings(body=""),
                worktree=wt,
            ),
            budget=GENEROUS_CLOCK,
        )
    )

    assert t.killed is None
    assert not hasattr(t, "peak_context_tokens")
    assert t.consumption.consumed_tokens == 0
    assert t.commits == 0
    assert classify_implementer(t) is Outcome.IMPASSE
