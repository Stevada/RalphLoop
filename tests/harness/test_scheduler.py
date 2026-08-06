"""The scheduler, against the fakes.

Everything a stand-in Implementer *can* do is tested end-to-end against a real repo, in `test_parallel.py`
and `test_quarantine.py`, and that is where it belongs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph.harness import Actor, Outcome, TokenConsumption, Verdict
from ralph.issues import SubIssueId, SubIssueState
from ralph.mergequeue import MergeQueue
from ralph.ports import Budget
from ralph.runlog import EventKind
from ralph.scheduler import Scheduler
from tests.builders import graph_of, telemetry, verdict
from tests.fakes import (
    FakeEditor,
    FakeGit,
    FakeImplementer,
    FakeIntegrator,
    FakeIssueStore,
    FakeRunLog,
    FakeTestRunner,
)

REPO = Path("/repo")


def terminal_editor() -> FakeEditor:
    return FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.PLANNING_DEFECT))])


def scheduler_over(
    store: FakeIssueStore,
    implementer: FakeImplementer,
    git: FakeGit,
    log: FakeRunLog,
    editor: FakeEditor | None = None,
    integrator: FakeIntegrator | None = None,
) -> tuple[Scheduler, FakeTestRunner]:
    runner = FakeTestRunner()
    scheduler = Scheduler(
        repo=REPO,
        git=git,
        store=store,
        run_log=log,
        implementer=implementer,
        editor=editor or terminal_editor(),
        merge_queue=MergeQueue(
            git=git,
            runner=runner,
            integration="integration",
            integrator=integrator or FakeIntegrator(git=git),
        ),
        integration="integration",
        budget=Budget(),
    )
    return scheduler, runner


async def test_an_infra_failure_goes_straight_to_a_human_and_never_to_the_editor() -> None:
    """An infra failure is not a failure the Editor can help with, and it spends no cycle."""
    store = FakeIssueStore(
        graph=graph_of({"01": [], "02": ["01"]}),
        states={SubIssueId("01"): SubIssueState.READY, SubIssueId("02"): SubIssueState.READY},
    )
    implementer = FakeImplementer(
        scripted=[telemetry(killed="wall-clock", exit_code=124, commits=0)]
    )
    git, log = FakeGit(head="integration"), FakeRunLog()

    scheduler, runner = scheduler_over(store, implementer, git, log)
    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INFRA_FAILED}
    assert git.fast_forwarded == []  # it never reached the merge queue
    assert runner.runs == []  # and the scheduler did not run a post-session suite
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

    scheduler, runner = scheduler_over(store, implementer, git, log)
    await scheduler.run()

    assert git.moved == [("ralph/01", REPO / ".worktrees" / "failed" / "01")]
    assert runner.runs == []


async def test_a_landed_sub_issue_leaves_its_worktree_where_it_was() -> None:
    """The control for the test above: only failures are moved. A run that shuffled every worktree
    into `failed/` would pass the assertion above and be nonsense."""
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    git, log = FakeGit(head="integration"), FakeRunLog()

    scheduler, runner = scheduler_over(store, FakeImplementer(), git, log)
    report = await scheduler.run()

    assert report.landed == (SubIssueId("01"),)
    assert git.moved == []
    assert runner.runs == [REPO / ".worktrees" / "active" / "01"]


async def test_a_merge_conflict_is_reconciled_and_lands_without_reaching_the_editor() -> None:
    """The Integrator answers it inside the queue, and the sub-issue lands as if nothing happened.

    One merge, not two: the queue never releases the lock, so there is no second trip through it.
    """
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(scripted=[telemetry()])
    editor = terminal_editor()
    git = FakeGit(head="integration", merge_conflicts={"ralph/01"})
    integrator = FakeIntegrator(
        git=git, scripted=[telemetry(consumption=TokenConsumption.total_only(7_000))]
    )

    scheduler, runner = scheduler_over(store, implementer, git, FakeRunLog(), editor, integrator)
    report = await scheduler.run()

    assert report.landed == (SubIssueId("01"),)
    assert report.clean
    assert git.merges == [("ralph/01", "integration")]
    assert len(implementer.calls) == 1
    assert len(integrator.calls) == 1
    assert editor.calls == []
    assert runner.runs == [REPO / ".worktrees" / "active" / "01"]
    # Billed, though it changed nothing about the outcome.
    assert [record.actor for _, record in store.consumption_records] == [
        Actor.IMPLEMENTER,
        Actor.INTEGRATOR,
    ]
    integrator_record = store.consumption_records[1][1]
    assert integrator_record.consumption.consumed_tokens == 7_000


async def test_a_reconciliation_that_succeeded_is_closed_in_the_log_like_any_other_session() -> None:
    """A `session-started` with no `session-finished` is a session the log says never ended.

    The failing reconciliation used to be the only one that got closed, so the log's account of the
    Integrator depended on whether it had worked.
    """
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    git = FakeGit(head="integration", merge_conflicts={"ralph/01"})
    run_log = FakeRunLog()

    scheduler, _ = scheduler_over(
        store,
        FakeImplementer(scripted=[telemetry()]),
        git,
        run_log,
        terminal_editor(),
        FakeIntegrator(git=git, scripted=[telemetry()]),
    )
    await scheduler.run()

    integrator = [e for e in run_log.events() if e.actor is Actor.INTEGRATOR]
    assert [e.kind for e in integrator] == [EventKind.SESSION_STARTED, EventKind.SESSION_FINISHED]
    assert integrator[1].details is Outcome.SUCCESS


async def test_a_reconciliation_is_stamped_from_its_own_clock_not_from_when_it_was_written() -> None:
    """The queue holds the merge lock for the whole reconciliation, so both events are written
    after it is over. Stamped `now`, a two-minute Integrator reads as instantaneous — which in the
    log is indistinguishable from one that died on startup."""
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    git = FakeGit(head="integration", merge_conflicts={"ralph/01"})
    run_log = FakeRunLog()

    scheduler, _ = scheduler_over(
        store,
        FakeImplementer(scripted=[telemetry()]),
        git,
        run_log,
        terminal_editor(),
        FakeIntegrator(git=git, scripted=[telemetry(wall_clock_s=120.0)]),
    )
    await scheduler.run()

    started, finished = (e for e in run_log.events() if e.actor is Actor.INTEGRATOR)
    assert (finished.ts - started.ts).total_seconds() == pytest.approx(120.0, abs=1.0)


async def test_an_unreconciled_conflict_goes_to_a_human_and_never_to_the_editor() -> None:
    """No spec was wrong, so there is nothing for an Editor to rewrite. The escalation carries the
    **Integrator's** outcome, not the Implementer's: the Implementer delivered."""
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(scripted=[telemetry()])
    editor = terminal_editor()
    git = FakeGit(head="integration", merge_conflicts={"ralph/01"})
    integrator = FakeIntegrator(git=git, resolves=False)

    scheduler, runner = scheduler_over(store, implementer, git, FakeRunLog(), editor, integrator)
    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert len(integrator.calls) == 1
    assert editor.calls == []
    assert runner.runs == [], "the suite gate is downstream of a merge that never finished"
    assert git.moved == [("ralph/01", REPO / ".worktrees" / "failed" / "01")]


async def test_a_crashed_integrator_session_goes_straight_to_a_human() -> None:
    """A killed Integrator is `infra-failed`, not a conflict nobody could resolve. Same
    destination, different sentence in the notification — and the second one sends a human to the
    wrong place."""
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(scripted=[telemetry()])
    editor = terminal_editor()
    git = FakeGit(head="integration", merge_conflicts={"ralph/01"})
    integrator = FakeIntegrator(
        git=git, resolves=False, scripted=[telemetry(killed="wall-clock", exit_code=124, commits=0)]
    )

    scheduler, _ = scheduler_over(store, implementer, git, FakeRunLog(), editor, integrator)
    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INFRA_FAILED}
    assert editor.calls == []
    assert git.moved == [("ralph/01", REPO / ".worktrees" / "failed" / "01")]


async def test_a_red_suite_after_reconciliation_still_reaches_the_editor() -> None:
    """The conflict was textual and got resolved; the tree is still broken. That is a *semantic*
    failure, which is the Editor's, and the reconciliation does not buy an exemption from the gate.
    """
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(scripted=[telemetry()])
    editor = terminal_editor()
    git = FakeGit(head="integration", merge_conflicts={"ralph/01"})
    integrator = FakeIntegrator(git=git)

    scheduler, runner = scheduler_over(store, implementer, git, FakeRunLog(), editor, integrator)
    runner.red_in.add(REPO / ".worktrees" / "active" / "01")

    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert len(integrator.calls) == 1
    assert len(editor.calls) == 1
    _, failure, _ = editor.calls[0]
    assert failure.integration_detail == "suite-red"
    assert failure.suite is not None and not failure.suite.green
    # Still billed, even though the sub-issue went on to fail for an unrelated reason.
    assert [record.actor for _, record in store.consumption_records] == [
        Actor.IMPLEMENTER,
        Actor.INTEGRATOR,
        Actor.EDITOR,
    ]


async def test_a_merge_queue_failure_that_is_not_a_conflict_never_opens_an_integrator() -> None:
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(scripted=[telemetry()])
    editor = terminal_editor()
    git = FakeGit(head="integration", ff_refuses={"ralph/01"})
    integrator = FakeIntegrator(git=git)

    scheduler, _ = scheduler_over(store, implementer, git, FakeRunLog(), editor, integrator)
    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert integrator.calls == []
