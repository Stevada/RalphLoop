"""**`ralph run` works.**

The tracer bullet. Every test here goes through `ralph.cli.run` — the real CLI, the real
filesystem store, real git, real worktrees, the real merge queue, a real suite, and a real agent
subprocess. Failure cases inject a scripted Editor so the test does not call a real model.

The only thing that is not real is the agent's intelligence, and that is the one thing the harness
was never trusting anyway.
"""

from __future__ import annotations

import json
import sys

import pytest

from ralph.adapters.git import GitCli
from ralph.cli import run
from ralph.harness import Outcome, Verdict
from ralph.issues import SubIssueId, SubIssueState
from ralph.ports import Budget
from ralph.scheduler import BaseIsRed
from tests.builders import telemetry, verdict as editor_verdict
from tests.fakes import FakeEditor
from tests.testbed import (
    Behaviour,
    StandInAgent,
    TargetRepo,
    make_options,
    stand_in_implementer as stand_in,
)


def terminal_editor() -> FakeEditor:
    return FakeEditor(
        scripted=[(telemetry(commits=0), editor_verdict(Verdict.PLANNING_DEFECT))]
    )


async def test_ralph_run_lands_a_sub_issue_end_to_end(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        options=make_options(),
    )

    # Both sub-issues in the fixture's graph land, and 02 could only start once 01 had.
    assert report.landed == (SubIssueId("01"), SubIssueId("02"))
    assert report.clean

    # The work is on the integration branch.
    assert (repo.path / "feature_01.py").exists()
    assert (repo.path / "feature_02.py").exists()
    assert repo.commit_count("integration") == 3

    # The history is linear, and the suite is green on it.
    assert repo.git("log", "--merges", "--oneline", "integration") == ""
    assert repo.run_suite() is True

    # `Status: landed` — written after the fast-forward, by the harness, in the file.
    assert "Status: landed" in (repo.issues_dir / "01-first.md").read_text()
    assert "Status: landed" in (repo.issues_dir / "02-second.md").read_text()


async def test_the_run_log_tells_the_true_story_in_order(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        options=make_options(),
    )

    lines = [json.loads(x) for x in (repo.path / ".scratch" / "run.jsonl").read_text().splitlines()]
    story = [(e["sub_issue"], e["actor"], e["kind"], e["details"]) for e in lines]

    # Every successful session says whose it was. The Editor's own lines are added only when an
    # Implementer failure routes to adjudication, as covered in `test_editor_loop.py`.
    assert story == [
        ("01", "implementer", "session-started", "in-progress"),
        ("01", "implementer", "session-finished", "success"),
        ("01", "implementer", "sub-issue-closed", "landed"),  # after the fast-forward
        ("02", "implementer", "session-started", "in-progress"),
        ("02", "implementer", "session-finished", "success"),
        ("02", "implementer", "sub-issue-closed", "landed"),
    ]
    assert all(set(e) == {"ts", "sub_issue", "actor", "kind", "details"} for e in lines)


async def test_a_red_base_aborts_the_run_before_a_single_agent_starts(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Fatal, not a warning. Every session dispatched into a broken base would fail for a reason
    that has nothing to do with its sub-issue — and the harness would pay an Editor to diagnose
    each one."""
    (repo.path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a * b\n")
    repo.git("add", "-A")
    repo.git("commit", "-m", "break the base")

    with pytest.raises(BaseIsRed):
        await run(
            repo.path,
            None,
            implementer=stand_in(agent, Behaviour.SUCCEED),
            options=make_options(),
        )

    assert not repo.branch_exists("ralph/01")  # zero agents started
    assert not (repo.path / ".scratch" / "run.jsonl").exists()


async def test_a_failed_install_aborts_the_run_before_a_single_agent_starts(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    options = make_options(install_cmd=("false",))

    with pytest.raises(Exception, match="exited 1"):
        await run(repo.path, None, implementer=stand_in(agent, Behaviour.SUCCEED), options=options)

    assert not repo.branch_exists("ralph/01")


async def test_the_install_runs_once_in_the_base_checkout_never_per_worktree(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Two sub-issues, two worktrees, one install. N worktrees must not mean N installs of the
    same tree, with the first failure discovered N times."""
    ledger = repo.path.parent / "installs"
    installer = repo.path.parent / "install.py"
    installer.write_text(f"open({str(ledger)!r}, 'a').write('x')\n")
    options = make_options(install_cmd=(sys.executable, str(installer)))

    report = await run(repo.path, None, implementer=stand_in(agent, Behaviour.SUCCEED), options=options)

    assert len(report.landed) == 2
    assert ledger.read_text() == "x"


async def test_an_undeclared_impasse_session_does_not_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """It exits 0 and commits nothing. The prototype called that a skip and moved on."""
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.COMMIT_NOTHING),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert report.landed == ()
    assert report.failed == {SubIssueId("01"): Outcome.IMPASSE}
    assert repo.commit_count("integration") == 1
    assert "Status: needs-human" in (repo.issues_dir / "01-first.md").read_text()

    # 02 is blocked by 01, which never landed, so it never became eligible and never got a turn.
    # Nothing was written to say so — quarantine-and-drain needs no `skipped` state.
    assert not repo.branch_exists("ralph/02")
    assert "Status: ready" in (repo.issues_dir / "02-second.md").read_text()


async def test_a_session_that_commits_a_red_suite_does_not_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The dangerous one: it exits 0, it committed, and it is broken. The harness runs the suite —
    the model's exit code is its opinion."""
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.RED_SUITE),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert report.failed == {SubIssueId("01"): Outcome.IMPASSE}
    assert repo.commit_count("integration") == 1
    assert repo.run_suite() is True  # integration was never touched, so it is still green


async def test_a_hanging_session_is_killed_and_does_not_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    report = await run(
        repo.path,
        None,
        budget=Budget(wall_clock_s=1.0),
        implementer=stand_in(agent, Behaviour.HANG),
        options=make_options(),
    )

    assert report.failed == {SubIssueId("01"): Outcome.INFRA_FAILED}
    assert repo.commit_count("integration") == 1


async def test_an_impasse_does_not_land_and_is_recorded(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.IMPASSE),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert report.failed == {SubIssueId("01"): Outcome.IMPASSE}
    assert ("01", "session-finished", "impasse") in [
        (e["sub_issue"], e["kind"], e["details"])
        for e in (
            json.loads(x)
            for x in (repo.path / ".scratch" / "run.jsonl").read_text().splitlines()
        )
    ]


async def test_a_read_only_issue_store_does_not_crash_the_run(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The store is best-effort by contract — a run must not die because the tracker was
    unreachable. The run log is the authoritative record, and it still has the truth."""
    (repo.issues_dir / "01-first.md").chmod(0o444)
    (repo.issues_dir / "02-second.md").chmod(0o444)

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        options=make_options(),
    )

    assert report.landed == (SubIssueId("01"), SubIssueId("02"))
    assert "Status: ready" in (repo.issues_dir / "01-first.md").read_text()  # the mirror failed
    assert (repo.path / "feature_01.py").exists()  # the work landed anyway

    events = json.loads(
        "[" + ",".join((repo.path / ".scratch" / "run.jsonl").read_text().splitlines()) + "]"
    )
    assert sum(e["details"] == SubIssueState.LANDED.value for e in events) == 2


async def test_the_worktree_branch_carries_the_sub_issue_id(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        options=make_options(),
    )

    assert GitCli(repo=repo.path).commits_between("main", "ralph/01") >= 1
    assert (repo.path / ".worktrees" / "active" / "01").is_dir()
