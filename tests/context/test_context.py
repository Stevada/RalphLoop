"""The wall-clock bound and the context-metering symbols retained during the ceiling migration."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ralph.adapters.context import Bound, ContextMeter, run_bounded, tail
from ralph.adapters.git import GitCli
from ralph.adapters.session import SubprocessImplementer
from ralph.harness import Outcome, SuiteResult, classify_implementer
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext, Worktree
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


async def test_a_session_that_leaves_the_old_smart_zone_runs_to_completion() -> None:
    proc = await spawn(AT_ONCE)
    climbing = FakeContextSource([observation(60_000), observation(130_000)])

    bound = await run_bounded(proc, climbing, SMART_ZONE)

    assert bound == Bound(killed=None, peak_context_tokens=0, consumed_tokens=0)
    assert proc.returncode == 0


async def test_the_context_source_is_not_watched_for_consumption_either() -> None:
    proc = await spawn(AT_ONCE)
    climbing = [observation(60_000, consumed=spent) for spent in range(100_000, 600_001, 100_000)]

    bound = await run_bounded(proc, FakeContextSource(climbing), SMART_ZONE)

    assert bound == Bound(killed=None, peak_context_tokens=0, consumed_tokens=0)


async def test_a_session_inside_the_old_smart_zone_runs_to_completion() -> None:
    proc = await spawn(AT_ONCE)

    bound = await run_bounded(proc, FakeContextSource([observation(118_000)]), SMART_ZONE)

    assert bound == Bound(killed=None, peak_context_tokens=0, consumed_tokens=0)
    assert proc.returncode == 0


async def test_the_clock_catches_a_stuck_session() -> None:
    proc = await spawn(FOREVER)
    flat = FakeContextSource([observation(1_000)] * 3)

    bound = await run_bounded(proc, flat, Budget(max_context_tokens=CEILING, wall_clock_s=0.3))

    assert bound.killed == "wall-clock"
    assert bound.peak_context_tokens == 0
    assert proc.returncode is not None


async def test_an_agent_with_no_context_signal_is_still_bounded_on_the_clock() -> None:
    """`source=None` is not "unbounded". It is the stand-in agent, and any bare `RALPH_AGENT_CMD`:
    a session nobody is metering, whose peak is honestly reported as zero rather than invented."""
    proc = await spawn(FOREVER)

    bound = await run_bounded(proc, None, Budget(wall_clock_s=0.3))

    assert bound == Bound(killed="wall-clock", peak_context_tokens=0, consumed_tokens=0)


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


async def test_the_tail_stops_on_an_empty_read_and_never_on_the_event_alone(
    tmp_path: Path,
) -> None:
    """A session that ended before the tail got a turn still has its whole rollout file read. The
    tail stops when a read comes back empty, and only *then* asks whether the session is over — in
    that order, so it can never stop with unread lines behind it. Checking the event first would
    drop the last model call of every session: the one nearest the ceiling.
    """
    path = tmp_path / "rollout.jsonl"
    path.write_text("first\nsecond\nthird\n")
    until = asyncio.Event()
    until.set()  # the session is already over before we read a byte

    lines = [line async for line in tail(path, until=until, poll_s=0.01)]

    assert lines == ["first", "second", "third"]


# ── and out the other side, as an Outcome ────────────────────────────────────────────────────


def test_a_ceiling_kill_classifies_as_ceiling_exceeded_not_infra_failed() -> None:
    """Both exit non-zero, and downstream they are indistinguishable unless separated here. One is
    a human's problem — the sub-issue is too big. The other is a bug in the harness. Reporting the
    first as the second sends the human to read our code instead of their brief.
    """
    killed = telemetry(exit_code=-9, killed="ceiling", peak_context_tokens=131_000, commits=0)

    assert classify_implementer(killed, GREEN) is Outcome.CEILING_EXCEEDED


async def test_context_sources_no_longer_reach_real_session_telemetry(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    def fail_if_constructed(transcript: object, worktree: Worktree) -> FakeContextSource:
        del transcript, worktree
        raise AssertionError("run_session must not construct a ContextSource")

    implementer = SubprocessImplementer(
        build_argv=lambda brief, findings, worktree: (sys.executable, "-c", AT_ONCE),
        context=fail_if_constructed,
    )

    t = await implementer.run(
        SessionContext(
            brief=Brief(body="build it"),
            findings=Findings(body=""),
            worktree=wt,
            budget=SMART_ZONE,
        )
    )

    assert t.killed is None
    assert t.peak_context_tokens == 0
    assert t.consumed_tokens == 0
    assert t.commits == 0
    assert classify_implementer(t, GREEN) is Outcome.IMPASSE
