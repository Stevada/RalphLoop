"""The smart-zone ceiling: what it gates on, and — more importantly — what it does not.

The one mistake this file exists to catch is a ceiling on **consumption**. Context and consumption
arrive in the same event, adjacent, with names that read alike, and a ceiling built on the wrong
one is not merely inaccurate — it is inverted. It would kill a long, cheap, perfectly focused
session and wave through a bloated one. Several tests below would pass with either number wired in;
`test_it_never_trips_on_consumption` is the one that would not.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from ralph.adapters.context import Bound, ContextMeter, run_bounded, tail
from ralph.adapters.git import GitCli
from ralph.adapters.session import SubprocessImplementer
from ralph.domain import Brief, Findings, Outcome, SuiteResult, classify_implementer
from ralph.ports import Budget, ContextSource, Observation
from tests.builders import observation, telemetry
from tests.fakes import FakeContextSource
from tests.testbed import TargetRepo

CEILING = 120_000
SMART_ZONE = Budget(max_context_tokens=CEILING, wall_clock_s=10.0)

GREEN = SuiteResult(green=True, output="", duration_s=0.0)


async def spawn(*python: str) -> asyncio.subprocess.Process:
    """A real process to bound. The ceiling kills processes; a mock would not notice if it didn't."""
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", "".join(python), stdout=asyncio.subprocess.DEVNULL
    )


FOREVER = "import time\nwhile True: time.sleep(0.05)\n"
AT_ONCE = "pass\n"


# ── the meter ────────────────────────────────────────────────────────────────────────────────


def test_the_meter_records_the_high_water_mark_whether_or_not_it_tripped() -> None:
    """A session that peaked at 118k did not fail. That it nearly did is the most useful number in
    its telemetry, and a meter that only remembered failures would throw it away."""
    meter = ContextMeter(ceiling=CEILING)
    for size in (10_000, 118_000, 90_000):
        meter.observe(observation(size))

    assert meter.peak == 118_000
    assert not meter.exceeded


def test_the_meter_reads_the_peak_not_the_last_observation() -> None:
    """Compaction must not be allowed to hide the crossing. A model that touched 130k has already
    done its bad thinking; dropping back to 90k afterwards does not un-think it."""
    meter = ContextMeter(ceiling=CEILING)
    meter.observe(observation(130_000))
    meter.observe(observation(90_000))

    assert meter.exceeded
    assert meter.peak == 130_000


def test_the_meter_keeps_the_last_rate_limit_it_was_told() -> None:
    """Logged, never gated: free early warning for the 429 that would otherwise arrive as an
    unexplained `infra-failed`."""
    meter = ContextMeter(ceiling=CEILING)
    meter.observe(observation(1_000, rate_limit=12.5))
    meter.observe(observation(2_000, rate_limit=None))  # Codex omits it on some events

    assert meter.rate_limit_used_percent == 12.5


# ── the kill loop ────────────────────────────────────────────────────────────────────────────


async def test_a_session_that_leaves_the_smart_zone_is_killed() -> None:
    proc = await spawn(FOREVER)

    climbing = FakeContextSource([observation(60_000), observation(130_000)])

    bound = await run_bounded(proc, climbing, SMART_ZONE)

    assert bound.killed == "ceiling"
    assert bound.peak_context_tokens >= CEILING
    assert proc.returncode is not None  # really dead, not merely reported dead


async def test_it_never_trips_on_consumption() -> None:
    """**The test this file is for.** Context flat at 60k — comfortably inside the smart zone —
    while cumulative consumption climbs past 500k. A ceiling wired to the wrong number kills this
    session. The right one never even considers it.
    """
    proc = await spawn(AT_ONCE)
    climbing = [observation(60_000, consumed=spent) for spent in range(100_000, 600_001, 100_000)]

    bound = await run_bounded(proc, FakeContextSource(climbing), SMART_ZONE)

    assert bound.killed is None
    assert bound.peak_context_tokens == 60_000
    assert bound.consumed_tokens == 600_000  # recorded in full, and gated on not at all


async def test_a_session_inside_the_smart_zone_runs_to_completion() -> None:
    proc = await spawn(AT_ONCE)

    bound = await run_bounded(proc, FakeContextSource([observation(118_000)]), SMART_ZONE)

    assert bound == Bound(killed=None, peak_context_tokens=118_000, consumed_tokens=50_000)
    assert proc.returncode == 0


async def test_the_clock_catches_what_the_ceiling_cannot() -> None:
    """A session spinning on a failing suite has a *flat* context. It will never trip the ceiling
    however long it runs — and it is the failure mode the ceiling most looks like it should catch.
    Only the clock stops it, which is why there are two bounds and not one.
    """
    proc = await spawn(FOREVER)
    flat = FakeContextSource([observation(1_000)] * 3)

    bound = await run_bounded(proc, flat, Budget(max_context_tokens=CEILING, wall_clock_s=0.3))

    assert bound.killed == "wall-clock"
    assert bound.peak_context_tokens == 1_000  # never came close
    assert proc.returncode is not None


async def test_an_agent_with_no_context_signal_is_still_bounded_on_the_clock() -> None:
    """`source=None` is not "unbounded". It is the stand-in agent, and any bare `RALPH_AGENT_CMD`:
    a session nobody is metering, whose peak is honestly reported as zero rather than invented."""
    proc = await spawn(FOREVER)

    bound = await run_bounded(proc, None, Budget(wall_clock_s=0.3))

    assert bound == Bound(killed="wall-clock", peak_context_tokens=0, consumed_tokens=0)


async def test_the_rate_limit_is_logged_and_nothing_is_gated_on_it(
    caplog: pytest.LogCaptureFixture
) -> None:
    proc = await spawn(AT_ONCE)
    caplog.set_level("INFO", logger="ralph.adapters.context")

    bound = await run_bounded(
        proc, FakeContextSource([observation(1_000, rate_limit=97.5)]), SMART_ZONE
    )

    assert "97.5" in caplog.text
    assert bound.killed is None  # 97.5% of the rate limit gone, and the session ran on


async def test_the_source_is_closed_when_the_ceiling_fires(tmp_path: Path) -> None:
    """The kill breaks out of the loop mid-stream, and a real source is holding a file handle open
    at that moment. If `run_bounded` did not close it, a run of hundreds of sessions would leak one
    descriptor per kill — and the leak would only show up in production."""
    closed = asyncio.Event()

    class Watchful:
        async def observations(self) -> AsyncGenerator[Observation, None]:
            try:
                yield observation(130_000)
                yield observation(10_000)
            finally:
                closed.set()

    source: ContextSource = Watchful()
    proc = await spawn(FOREVER)

    await run_bounded(proc, source, SMART_ZONE)

    assert closed.is_set()


# ── the tail ─────────────────────────────────────────────────────────────────────────────────


async def test_the_tail_yields_lines_while_the_file_is_still_being_written(tmp_path: Path) -> None:
    """*While*, not *after*. Reading the rollout file at exit would mean metering a session only
    once it was too late to stop it."""
    path = tmp_path / "rollout.jsonl"
    path.write_text("first\n")
    until = asyncio.Event()
    seen: list[str] = []

    async def append_slowly() -> None:
        for line in ("second", "third"):
            await asyncio.sleep(0.05)
            with path.open("a") as f:
                f.write(f"{line}\n")
        await asyncio.sleep(0.05)
        until.set()

    writer = asyncio.create_task(append_slowly())
    async for line in tail(path, until=until, poll_s=0.01):
        seen.append(line)
        if len(seen) == 2:
            assert not until.is_set()  # we saw the second line before the session ended
    await writer

    assert seen == ["first", "second", "third"]


async def test_the_tail_reads_what_was_written_after_the_session_went_quiet(
    tmp_path: Path
) -> None:
    """`until` is stdout closing, which happens a moment *before* the process exits. A tail that
    stopped dead on the event would drop the last model call of every session — the one nearest the
    ceiling, and the only one that could still have tripped it."""
    path = tmp_path / "rollout.jsonl"
    path.write_text("early\n")
    until = asyncio.Event()
    until.set()
    path.write_text("early\nlate\n")

    assert [line async for line in tail(path, until=until, poll_s=0.01)] == ["early", "late"]


# ── and out the other side, as an Outcome ────────────────────────────────────────────────────


def test_a_ceiling_kill_classifies_as_ceiling_exceeded_not_infra_failed() -> None:
    """Both exit non-zero, and downstream they are indistinguishable unless separated here. One is
    a human's problem — the sub-issue is too big. The other is a bug in the harness. Reporting the
    first as the second sends the human to read our code instead of their brief.
    """
    killed = telemetry(exit_code=-9, killed="ceiling", peak_context_tokens=131_000, commits=0)

    assert classify_implementer(killed, GREEN) is Outcome.CEILING_EXCEEDED


async def test_the_ceiling_reaches_the_telemetry_of_a_real_session(repo: TargetRepo) -> None:
    """End to end through `run_agent`: a real subprocess, killed by a real ceiling, and the peak
    lands in the `SessionTelemetry` the scheduler will classify."""
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    implementer = SubprocessImplementer(
        build_argv=lambda brief, findings, worktree: (sys.executable, "-c", FOREVER),
        context=lambda transcript: FakeContextSource([observation(200_000, consumed=210_000)]),
    )

    t = await implementer.run(Brief(body="build it"), Findings(body=""), wt, SMART_ZONE)

    assert t.killed == "ceiling"
    assert t.peak_context_tokens == 200_000
    assert t.consumed_tokens == 210_000
    assert t.commits == 0  # it never got the chance
    assert classify_implementer(t, GREEN) is Outcome.CEILING_EXCEEDED
