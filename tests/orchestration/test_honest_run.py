"""**The honest run.** Everything else has been verified in pieces; this proves it whole.

One graph, the shape a real phase actually has — a contract sub-issue, two that build on it in
parallel, and one that integrates them:

    01 ──┬── 02 ──┬── 04
         └── 03 ──┘

against a real git repository, with real worktrees, a real merge queue, a real suite, and a real
agent subprocess. Failure cases inject a scripted Editor so the test does not call a real model.

This is not the moment the system first comes together — that was #04, and every ticket since has
kept it working. There is no big-bang integration here. What is new is the *whole* claim, asserted
in one place: every sub-issue lands, the history is linear, the suite is green on the integration
branch, and the run log tells the truth about what happened in what order.
"""

from __future__ import annotations

import json

from ralph.cli import render, run
from ralph.harness import Outcome, Verdict
from ralph.issues import SubIssueId
from tests.builders import telemetry, verdict as editor_verdict
from tests.fakes import FakeEditor
from tests.testbed import (
    Behaviour,
    PARENT_ISSUE_NAME,
    StandInAgent,
    TargetRepo,
    behaviour_spec,
    make_options,
    stand_in_implementer as stand_in,
    unengaged_editor,
)

# A contract, two in parallel behind it, one integrating both. The smallest graph in which
# "in parallel" and "in order" are both claims, and can therefore both be wrong.
PHASE = {"01": [], "02": ["01"], "03": ["01"], "04": ["02", "03"]}


def terminal_editor() -> FakeEditor:
    return FakeEditor(
        scripted=[(telemetry(commits=0), editor_verdict(Verdict.PLANNING_DEFECT))]
    )


def events(repo: TargetRepo) -> list[dict[str, str]]:
    lines = (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines]


def story(repo: TargetRepo) -> list[tuple[str, str, str]]:
    return [(e["sub_issue"], e["kind"], e["details"]) for e in events(repo)]


async def test_every_sub_issue_lands_and_the_history_is_linear(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    repo.write_graph(PHASE)

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert sorted(report.landed) == ["01", "02", "03", "04"]
    assert report.clean

    # Four sub-issues, four commits on top of the base and its graph commit — and **no merge
    # commits**. Four agents worked concurrently and the branch reads as though they had queued.
    assert repo.commit_count("integration") == 6
    assert repo.git("log", "--merges", "--oneline", "integration") == ""

    # The suite is green on the integration branch, run from the base checkout, after everything
    # landed. Not the harness's own word for it: pytest, again, on the tree that now exists.
    assert repo.run_suite() is True
    for id in PHASE:
        assert (repo.path / f"feature_{id}.py").exists()
        assert "Status: landed" in (repo.issues_dir / f"{id}-sub.md").read_text()


async def test_the_run_log_tells_the_true_story_in_order(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """*In order* is the whole claim, and it is not the same claim as *complete*.

    02 and 03 run concurrently, so their lines interleave and the exact sequence is not ours to
    predict. What the graph does determine is asserted here: 01 closes before either of them opens,
    and both of them land before 04 opens. A log that recorded 04 starting before its blockers
    landed would be describing a run that never happened.
    """
    repo.write_graph(PHASE)

    await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    lines = story(repo)
    at = lines.index

    for id in PHASE:
        assert at((id, "session-started", "in-progress")) < at(
            (id, "session-finished", "success")
        )
        # After the fast-forward, never before: `landed` is written once the work is on the branch.
        assert at((id, "session-finished", "success")) < at(
            (id, "sub-issue-closed", "landed")
        )

    assert at(("01", "sub-issue-closed", "landed")) < at(
        ("02", "session-started", "in-progress")
    )
    assert at(("01", "sub-issue-closed", "landed")) < at(
        ("03", "session-started", "in-progress")
    )
    assert at(("02", "sub-issue-closed", "landed")) < at(
        ("04", "session-started", "in-progress")
    )
    assert at(("03", "sub-issue-closed", "landed")) < at(
        ("04", "session-started", "in-progress")
    )


async def test_the_run_log_carries_no_spend_no_diffstat_and_no_test_output(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The run log answers *what happened, in what order*, and a reader who has to skim past a
    40-line pytest dump to find the next event is not being told a story. Those facts exist — they
    are in the failure report, which has a different reader."""
    repo.write_graph(PHASE)
    spec = behaviour_spec(Behaviour.SUCCEED, {"02": Behaviour.RED_SUITE})

    await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=terminal_editor(),
        options=make_options(),
    )

    raw = (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text()
    assert all(set(e) == {"ts", "sub_issue", "actor", "kind", "details"} for e in events(repo))
    for leak in ("token", "diffstat", "assert", "passed", "failed,"):
        assert leak not in raw


async def test_one_failure_strands_its_dependents_and_nothing_else(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """02 declares an impasse. 03 has nothing to do with it and lands; 04 stands behind both and
    never gets a turn. One notification comes out at the end, and it names the one thing to open."""
    repo.write_graph(PHASE)
    spec = behaviour_spec(Behaviour.SUCCEED, {"02": Behaviour.IMPASSE})

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert sorted(report.landed) == ["01", "03"]
    assert report.failed == {SubIssueId("02"): Outcome.IMPASSE}
    assert "Status: needs-human" in (repo.issues_dir / "02-sub.md").read_text()

    # The evidence is where the human was told it would be, and the branch is untouched.
    assert (repo.path / ".worktrees" / "failed" / PARENT_ISSUE_NAME / "02").is_dir()
    assert not (repo.path / "feature_02.py").exists()
    assert repo.run_suite() is True

    # 04 was never started: no branch, no worktree, no line in the log, and no state written to say
    # so. Its `ready` is the Planner's, exactly as they left it.
    assert not repo.branch_exists(f"ralph/{PARENT_ISSUE_NAME}_04")
    assert "04" not in {id for id, _, _ in story(repo)}
    assert "Status: ready" in (repo.issues_dir / "04-sub.md").read_text()

    # Exactly one notification, and it says which to open first and what it is holding up.
    assert len(report.notification.escalations) == 1
    rendered = render(report.notification)
    assert "02  impasse" in rendered
    assert "holding up: 04" in rendered


async def test_a_semantic_conflict_surfaces_on_the_second_to_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """**The one a green integration branch would have hidden.**

    Two sub-issues, no dependency between them, both green in their own worktrees, and touching
    different files — so there is no rebase conflict for git to catch. One renames `calculator.add`;
    the other adds a test that calls it. Whichever lands second is rebased cleanly onto a branch its
    own work no longer fits, and only the suite re-run *on the prospective merge* can see it.

    Which of the two loses the race is not asserted, because it is not determined: they are dispatched
    together and the merge lock decides. What is determined — and is the entire point — is that
    exactly one lands, the other comes back `integration-failed`, and the integration branch is green
    at the end rather than green-looking.
    """
    repo.write_graph({"01": [], "02": []})
    spec = behaviour_spec(Behaviour.RENAMES_THE_API, {"02": Behaviour.CALLS_THE_API})

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert len(report.landed) == 1
    assert list(report.failed.values()) == [Outcome.INTEGRATION_FAILED]

    # Not "the harness thinks it is green". The suite, run again, on the branch as it now stands.
    assert repo.run_suite() is True
    assert repo.git("log", "--merges", "--oneline", "integration") == ""

    loser = next(iter(report.failed))
    assert (repo.path / ".worktrees" / "failed" / PARENT_ISSUE_NAME / str(loser)).is_dir()
    assert "the merge queue: suite-red" in render(report.notification)
