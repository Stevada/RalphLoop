"""The scheduler, against the fakes — for the failures a real agent cannot yet be made to produce.

Everything a stand-in agent *can* do is tested end-to-end against a real repo, in `test_parallel.py`
and `test_quarantine.py`, and that is where it belongs. What is left here is the one outcome no
subprocess can fake today: `ceiling-exceeded`, which needs a context meter that arrives in #08.

Testing it now anyway is deliberate. The rule it proves — that the two outcomes the Editor never
sees go straight to a human — is the rule most likely to be quietly broken by whoever wires the
ceiling up, and this test will be sitting here waiting when they do.
"""

from __future__ import annotations

from pathlib import Path

from ralph.harness import Outcome
from ralph.issues import SubIssueId, SubIssueState
from ralph.mergequeue import MergeQueue
from ralph.ports import Budget
from ralph.scheduler import Scheduler
from tests.builders import graph_of, telemetry
from tests.fakes import (
    FakeGit,
    FakeImplementer,
    FakeIssueStore,
    FakeRunLog,
    FakeTestRunner,
)

REPO = Path("/repo")


def scheduler_over(
    store: FakeIssueStore, implementer: FakeImplementer, git: FakeGit, log: FakeRunLog
) -> Scheduler:
    runner = FakeTestRunner()
    return Scheduler(
        repo=REPO,
        git=git,
        store=store,
        run_log=log,
        runner=runner,
        implementer=implementer,
        merge_queue=MergeQueue(git=git, runner=runner, integration="integration"),
        integration="integration",
        budget=Budget(),
        concurrency=2,
    )


async def test_a_ceiling_kill_goes_straight_to_a_human_and_never_to_the_editor() -> None:
    """`ceiling-exceeded` is not a failure the Editor can help with. The session did not reason
    badly — it reasoned over too much, outside the smart zone, and the only fix is to re-cut the
    sub-issue. That is a Planner's job, and it spends no cycle."""
    store = FakeIssueStore(
        graph=graph_of({"01": [], "02": ["01"]}),
        states={SubIssueId("01"): SubIssueState.READY, SubIssueId("02"): SubIssueState.READY},
    )
    implementer = FakeImplementer(scripted=[telemetry(killed="ceiling", exit_code=137, commits=0)])
    git, log = FakeGit(head="integration"), FakeRunLog()

    report = await scheduler_over(store, implementer, git, log).run()

    assert report.failed == {SubIssueId("01"): Outcome.CEILING_EXCEEDED}
    assert git.merged == []  # it never reached the merge queue
    assert len(implementer.calls) == 1  # and it was never run a second time

    # 02 stands behind it, so it never got a turn — and carries no state saying so.
    assert report.landed == ()
    assert report.notification.escalations[0].stranded == (SubIssueId("02"),)
    assert SubIssueId("02") not in {e.sub_issue for e in log.events()}


async def test_the_quarantined_worktree_is_moved_out_of_the_way() -> None:
    """`.worktrees/failed/<id>` is where the notification tells a human to look, so it had better be
    where the worktree is."""
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(scripted=[telemetry(commits=0)])  # undeclared impasse
    git, log = FakeGit(head="integration"), FakeRunLog()

    await scheduler_over(store, implementer, git, log).run()

    assert git.moved == [("ralph/01", REPO / ".worktrees" / "failed" / "01")]


async def test_a_landed_sub_issue_leaves_its_worktree_where_it_was() -> None:
    """The control for the test above: only failures are moved. A run that shuffled every worktree
    into `failed/` would pass the assertion above and be nonsense."""
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    git, log = FakeGit(head="integration"), FakeRunLog()

    report = await scheduler_over(store, FakeImplementer(), git, log).run()

    assert report.landed == (SubIssueId("01"),)
    assert git.moved == []
