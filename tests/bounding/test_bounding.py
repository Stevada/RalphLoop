"""The wall-clock bound for running sessions."""

from __future__ import annotations

import asyncio
import sys

from ralph.adapters.runtime.bounding import Bound, run_bounded
from ralph.adapters.git import GitCli
from ralph.adapters.runtime.implementer import SubprocessImplementer
from ralph.harness import Outcome, SuiteResult, classify_implementer
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext
from tests.testbed import TargetRepo

SHORT_CLOCK = Budget(wall_clock_s=0.3)
GENEROUS_CLOCK = Budget(wall_clock_s=10.0)
GREEN = SuiteResult(green=True, output="", duration_s=0.0)


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

    assert bound == Bound(killed=None, consumed_tokens=0)
    assert proc.returncode == 0


async def test_the_clock_catches_a_stuck_session() -> None:
    proc = await spawn(FOREVER)

    bound = await run_bounded(proc, SHORT_CLOCK)

    assert bound == Bound(killed="wall-clock", consumed_tokens=0)
    assert proc.returncode is not None


async def test_real_session_telemetry_has_no_context_peak(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    implementer = SubprocessImplementer(
        build_argv=lambda brief, findings, worktree: (sys.executable, "-c", AT_ONCE),
    )

    t = await implementer.run(
        SessionContext(
            brief=Brief(body="build it"),
            findings=Findings(body=""),
            worktree=wt,
            budget=GENEROUS_CLOCK,
        )
    )

    assert t.killed is None
    assert not hasattr(t, "peak_context_tokens")
    assert t.consumed_tokens == 0
    assert t.commits == 0
    assert classify_implementer(t, GREEN) is Outcome.IMPASSE
