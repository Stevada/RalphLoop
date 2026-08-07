"""The merge queue, against real git.

The one claim worth testing hardest: the suite is re-run **in the worktree, after the merge**, on
the prospective merge result. Run it anywhere else and you have verified a tree you are not
landing — a green integration branch that is broken, which is the failure the harness exists to
make impossible.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from dataclasses import dataclass, field

from ralph.adapters.git import GitCli
from ralph.adapters.suite import SubprocessTestRunner
from ralph.harness import Outcome, SuiteResult
from ralph.mergequeue import Land, LandResult, MergeQueue
from ralph.ports import Budget, Integrator, Worktree
from tests.builders import candidate
from tests.fakes import FakeGit, FakeIntegrator, FakeTestRunner
from tests.testbed import (
    TEST_CMD,
    Behaviour,
    StandInAgent,
    StandInIntegrator,
    TargetRepo,
)


@dataclass(slots=True)
class OverlapWatchingRunner:
    """A `TestRunner` that reports the high-water mark of suite runs happening at once.

    The merge queue runs the suite *inside* the merge lock, so this is a direct read on the lock: if
    two lands ever overlap here, the lock leaked and the second merged a branch that was about
    to move under it.
    """

    peak: int = 0
    inside: int = 0
    runs: list[Path] = field(default_factory=list)

    async def run(self, dir: Path) -> SuiteResult:
        self.runs.append(dir)
        self.inside += 1
        self.peak = max(self.peak, self.inside)
        await asyncio.sleep(0.05)  # a real suspension point, inside the lock
        self.inside -= 1
        return SuiteResult(green=True, output="1 passed", duration_s=0.05)


def real_queue(
    repo: TargetRepo, integrator: Integrator | None = None
) -> tuple[MergeQueue, GitCli]:
    git = GitCli(repo=repo.path)
    runner = SubprocessTestRunner(cmd=TEST_CMD)
    queue = MergeQueue(
        git=git,
        runner=runner,
        integration="integration",
        integrator=integrator if integrator is not None else FakeIntegrator(),
        budget=Budget(),
    )
    return queue, git


def work(agent: StandInAgent, behaviour: Behaviour, tag: str, wt: Worktree) -> None:
    subprocess.run(agent.argv(behaviour, tag), cwd=wt.path, check=True, capture_output=True)


async def test_a_green_sub_issue_lands_and_the_history_stays_linear(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    queue, git = real_queue(repo)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    work(agent, Behaviour.SUCCEED, "01", wt)

    land = await queue.land(candidate(wt))

    assert land.result is LandResult.LANDED
    assert land.suite is not None and land.suite.green
    assert (repo.path / "feature_01.py").exists()
    assert repo.git("log", "--merges", "--oneline", "integration") == ""


async def test_a_conflicting_sibling_is_reconciled_and_lands(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The whole feature, against real git. 01 and 02 rewrite the same line; 01 wins the lock.
    02's merge conflicts, an Integrator is dispatched into the conflict, and the sub-issue lands
    **without leaving the queue** — one hold of the lock, start to finish."""
    resolver = StandInIntegrator()
    queue, git = real_queue(repo, resolver)
    left = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    right = git.add_worktree("ralph/02", repo.path / ".worktrees" / "active" / "02", "integration")
    work(agent, Behaviour.CONFLICT, "01", left)
    work(agent, Behaviour.CONFLICT, "02", right)

    assert (await queue.land(candidate(left))).result is LandResult.LANDED
    assert resolver.calls == [], "a clean landing must not open an Integrator session"

    land = await queue.land(candidate(right))

    assert land.result is LandResult.LANDED
    assert len(resolver.calls) == 1
    # Billed even though it succeeded and the sub-issue landed as if nothing had happened.
    assert land.integrator is not None
    assert land.suite is not None and land.suite.green
    assert repo.git("rev-parse", "integration") == repo.git("rev-parse", "ralph/02")
    # Both commits, in order: what the Implementer wrote, then what it took to make it land.
    assert repo.git("log", "-1", "--format=%s", "integration").startswith("reconcile")


async def test_an_unreconciled_conflict_goes_to_a_human_and_never_reaches_the_suite(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """VIR-85's failure, as a test. The session ran and the merge is still open — which is what the
    queue asks git, rather than asking the session or counting its commits. The branch it would have
    landed carries the Implementer's commits and nothing else, so a commit count reads exactly as it
    would on success."""
    queue, git = real_queue(repo, StandInIntegrator(resolves=False))
    left = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    right = git.add_worktree("ralph/02", repo.path / ".worktrees" / "active" / "02", "integration")
    work(agent, Behaviour.CONFLICT, "01", left)
    work(agent, Behaviour.CONFLICT, "02", right)
    assert (await queue.land(candidate(left))).result is LandResult.LANDED

    land = await queue.land(candidate(right))

    assert land.result is LandResult.CONFLICT_UNRESOLVED
    assert land.integrator_outcome is Outcome.INTEGRATION_FAILED
    assert land.suite is None  # it never got as far as running one, and does not pretend it did
    assert repo.git("rev-parse", "integration") == repo.git("rev-parse", "ralph/01")
    # The conflict is preserved, unresolved, for the human who now owns it.
    assert "<<<<<<<" in (right.path / "shared.py").read_text()


async def test_the_suite_runs_on_the_prospective_merge_not_on_the_worktree_as_it_was(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The semantic conflict. Each sub-issue is green in isolation; together they are red. Only a
    suite run *after* the merge can see it — and it is precisely what the Implementer could not
    have observed about itself.
    """
    queue, git = real_queue(repo)
    left = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    right = git.add_worktree("ralph/02", repo.path / ".worktrees" / "active" / "02", "integration")

    # 01 renames the function. 02 adds a test that calls it by its old name. Neither touches the
    # other's lines, so there is no textual conflict at all — and both are green alone.
    (left.path / "calculator.py").write_text("def plus(a: int, b: int) -> int:\n    return a + b\n")
    (left.path / "test_calculator.py").write_text(
        "from calculator import plus\n\n\ndef test_plus() -> None:\n    assert plus(1, 2) == 3\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=left.path, check=True)
    subprocess.run(["git", "commit", "-m", "rename add to plus"], cwd=left.path, check=True)

    (right.path / "test_extra.py").write_text(
        "from calculator import add\n\n\ndef test_add_again() -> None:\n    assert add(2, 2) == 4\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=right.path, check=True)
    subprocess.run(["git", "commit", "-m", "one more test for add"], cwd=right.path, check=True)

    assert (await queue.land(candidate(left))).result is LandResult.LANDED

    land = await queue.land(candidate(right))

    assert land.result is LandResult.SUITE_RED
    # And it hands back *that* suite result — the red one, from the prospective merge. The Editor
    # being told `integration-failed` alongside the green run 02 had in isolation would be a
    # contradiction the harness manufactured for it.
    assert land.suite is not None and not land.suite.green
    assert "ImportError" in land.suite.output or "cannot import name" in land.suite.output

    # The integration branch is exactly where 01 left it, and it is green.
    assert repo.git("rev-parse", "integration") == repo.git("rev-parse", "ralph/01")
    assert repo.run_suite() is True


async def test_the_land_aborts_if_the_base_repo_moved_out_from_under_it(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Fast-forwarding now would move a branch nobody asked us to move."""
    queue, git = real_queue(repo)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    work(agent, Behaviour.SUCCEED, "01", wt)
    repo.git("checkout", "main")

    assert (await queue.land(candidate(wt))).result is LandResult.HEAD_MOVED


async def test_a_refused_fast_forward_is_reported_never_papered_over() -> None:
    """Unreachable by construction — we just merged integration in, so integration is an
    ancestor. If git refuses anyway, something we believe about the repository is false. The one
    thing the queue must not do is reach for a merge commit, which would put an unverified tree on
    the integration branch: `git merge` does not fire the pre-commit hook.

    Driven with a fake git, because a real one cannot be made to refuse after a successful merge —
    which is the point.
    """
    git = FakeGit(head="integration", ff_refuses={"ralph/01"})
    queue = MergeQueue(
        git=git,
        runner=FakeTestRunner(),
        integration="integration",
        integrator=FakeIntegrator(),
        budget=Budget(),
    )
    wt = git.add_worktree("ralph/01", Path("/nowhere"), "integration")

    assert (await queue.land(candidate(wt))).result is LandResult.FF_REFUSED

    assert git.fast_forwarded == []


async def test_the_merge_lock_serializes_two_concurrent_lands() -> None:
    """Sub-issues run in parallel; they land **one at a time**. Two `land()` calls launched at the
    same instant must not interleave — the second's merge has to happen after the first's
    fast-forward, or it merges a branch that is about to move.

    A `TestRunner` that records the merge lock's own critical section is the only way to see this:
    if the lock leaked, two suite runs would overlap.
    """
    runner = OverlapWatchingRunner()
    git = FakeGit(head="integration")
    queue = MergeQueue(
        git=git,
        runner=runner,
        integration="integration",
        integrator=FakeIntegrator(),
        budget=Budget(),
    )
    left = git.add_worktree("ralph/01", Path("/nowhere/01"), "integration")
    right = git.add_worktree("ralph/02", Path("/nowhere/02"), "integration")

    results = await asyncio.gather(queue.land(candidate(left)), queue.land(candidate(right)))

    assert [r.result for r in results] == [LandResult.LANDED, LandResult.LANDED]
    assert runner.peak == 1, "two lands ran their suites at once — the merge lock did not hold"
    assert git.fast_forwarded == ["ralph/01", "ralph/02"]


def test_land_carries_no_suite_when_it_never_ran_one() -> None:
    """`suite=None` is not a default worth shrugging at: it is the difference between "the suite was
    green" and "there was no suite run", and a report that confused them would be lying."""
    assert Land(LandResult.HEAD_MOVED).suite is None
