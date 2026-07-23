"""The scheduler, against the fakes.

Everything a stand-in Implementer *can* do is tested end-to-end against a real repo, in `test_parallel.py`
and `test_quarantine.py`, and that is where it belongs.
"""

from __future__ import annotations

from pathlib import Path

from ralph.harness import Actor, Outcome, Verdict
from ralph.issues import SubIssueId, SubIssueState
from ralph.mergequeue import MergeQueue
from ralph.ports import Budget
from ralph.scheduler import Scheduler
from tests.builders import graph_of, telemetry, verdict
from tests.fakes import (
    FakeEditor,
    FakeGit,
    FakeImplementer,
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
) -> tuple[Scheduler, FakeTestRunner]:
    runner = FakeTestRunner()
    scheduler = Scheduler(
        repo=REPO,
        git=git,
        store=store,
        run_log=log,
        implementer=implementer,
        editor=editor or terminal_editor(),
        merge_queue=MergeQueue(git=git, runner=runner, integration="integration"),
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
    assert git.merged == []  # it never reached the merge queue
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


async def test_a_rebase_conflict_is_resolved_once_before_the_editor_is_involved() -> None:
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(
        scripted=[telemetry(resumable_identifier="resume-01")],
        conflict_scripted=[telemetry(consumed_tokens=7_000)],
    )
    editor = terminal_editor()
    git = FakeGit(
        head="integration",
        rebase_results={"ralph/01": [False, True]},
        conflict_resolution_rebase_results={"ralph/01": [False]},
    )
    log = FakeRunLog()

    scheduler, runner = scheduler_over(store, implementer, git, log, editor)
    report = await scheduler.run()

    assert report.landed == (SubIssueId("01"),)
    assert report.clean
    assert git.conflict_resolution_rebased == [("ralph/01", "integration")]
    assert git.rebased == [("ralph/01", "integration"), ("ralph/01", "integration")]
    assert len(implementer.calls) == 1
    assert implementer.resolve_conflict_calls == [
        (implementer.calls[0], "resume-01")
    ]
    assert editor.calls == []
    assert runner.runs == [REPO / ".worktrees" / "active" / "01"]
    assert [record.actor for _, record in store.consumption_records] == [
        Actor.IMPLEMENTER,
        Actor.IMPLEMENTER,
    ]


async def test_a_second_rebase_conflict_after_resolution_reaches_the_editor() -> None:
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(
        scripted=[telemetry(resumable_identifier="resume-01")],
        conflict_scripted=[telemetry()],
    )
    editor = terminal_editor()
    git = FakeGit(
        head="integration",
        rebase_results={"ralph/01": [False, False]},
        conflict_resolution_rebase_results={"ralph/01": [False]},
    )

    scheduler, _ = scheduler_over(store, implementer, git, FakeRunLog(), editor)
    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert len(implementer.resolve_conflict_calls) == 1
    assert len(editor.calls) == 1
    _, failure, must_be_terminal = editor.calls[0]
    assert failure.outcome is Outcome.INTEGRATION_FAILED
    assert failure.integration_detail == "rebase-conflict"
    assert must_be_terminal is False


async def test_a_red_retry_after_resolution_reaches_the_editor_with_suite_evidence() -> None:
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(
        scripted=[telemetry(resumable_identifier="resume-01")],
        conflict_scripted=[telemetry()],
    )
    editor = terminal_editor()
    git = FakeGit(
        head="integration",
        rebase_results={"ralph/01": [False, True]},
        conflict_resolution_rebase_results={"ralph/01": [False]},
    )
    scheduler, runner = scheduler_over(store, implementer, git, FakeRunLog(), editor)
    runner.red_in.add(REPO / ".worktrees" / "active" / "01")

    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert len(implementer.resolve_conflict_calls) == 1
    assert len(editor.calls) == 1
    _, failure, _ = editor.calls[0]
    assert failure.integration_detail == "suite-red"
    assert failure.suite is not None and not failure.suite.green


async def test_a_crashed_conflict_resolution_session_goes_straight_to_a_human() -> None:
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(
        scripted=[telemetry(resumable_identifier="resume-01")],
        conflict_scripted=[telemetry(killed="wall-clock", exit_code=124, commits=0)],
    )
    editor = terminal_editor()
    git = FakeGit(
        head="integration",
        rebase_results={"ralph/01": [False]},
        conflict_resolution_rebase_results={"ralph/01": [False]},
    )

    scheduler, _ = scheduler_over(store, implementer, git, FakeRunLog(), editor)
    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INFRA_FAILED}
    assert len(implementer.resolve_conflict_calls) == 1
    assert editor.calls == []
    assert git.moved == [("ralph/01", REPO / ".worktrees" / "failed" / "01")]


async def test_a_later_cycle_gets_its_own_single_conflict_resolution_session() -> None:
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(
        scripted=[
            telemetry(resumable_identifier="resume-01"),
            telemetry(resumable_identifier="resume-02"),
        ],
        conflict_scripted=[telemetry(), telemetry()],
    )
    editor = FakeEditor(
        scripted=[(telemetry(commits=0), verdict(Verdict.REVISE, spec="try again"))]
    )
    git = FakeGit(
        head="integration",
        rebase_results={"ralph/01": [False, False, False, True]},
        conflict_resolution_rebase_results={"ralph/01": [False, False]},
    )

    scheduler, _ = scheduler_over(store, implementer, git, FakeRunLog(), editor)
    report = await scheduler.run()

    assert report.landed == (SubIssueId("01"),)
    assert report.clean
    assert [identifier for _, identifier in implementer.resolve_conflict_calls] == [
        "resume-01",
        "resume-02",
    ]
    assert len(editor.calls) == 1


async def test_other_merge_queue_failures_do_not_use_conflict_resolution() -> None:
    store = FakeIssueStore(
        graph=graph_of({"01": []}), states={SubIssueId("01"): SubIssueState.READY}
    )
    implementer = FakeImplementer(scripted=[telemetry(resumable_identifier="resume-01")])
    editor = terminal_editor()
    git = FakeGit(head="integration", ff_refuses={"ralph/01"})

    scheduler, _ = scheduler_over(store, implementer, git, FakeRunLog(), editor)
    report = await scheduler.run()

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert implementer.resolve_conflict_calls == []
    assert git.conflict_resolution_rebased == []
