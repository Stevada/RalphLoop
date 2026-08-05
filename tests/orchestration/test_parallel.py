"""**Many land, in parallel, serialized by the merge lock.**

Sub-issues run in parallel but land one at a time. That is the whole trick, and every test here is
the real thing: real concurrent subprocesses, a real merge lock, real rebases onto a branch that is
moving under them.

The hard part of testing parallelism is proving it *happened*. A run that is secretly sequential
passes every correctness assertion you can write — the work still lands, the history is still
linear. So the stand-in agent keeps a ledger of when it was alive, and the tests read the
high-water mark off it. Nothing else can tell the difference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph.cli import run
from ralph.harness import Outcome, Verdict
from ralph.issues import SubIssueId
from tests.builders import telemetry, verdict as editor_verdict
from tests.fakes import FakeEditor
from tests.testbed import (
    LEDGER_ENV,
    PARENT_ISSUE_NAME,
    Behaviour,
    StandInAgent,
    TargetRepo,
    behaviour_spec,
    make_options,
    peak_concurrency,
    stand_in_implementer as stand_in,
    unengaged_editor,
)


def terminal_editor() -> FakeEditor:
    return FakeEditor(
        scripted=[(telemetry(commits=0), editor_verdict(Verdict.PLANNING_DEFECT))]
    )


def with_ledger(monkeypatch: pytest.MonkeyPatch, repo: TargetRepo) -> Path:
    """Where the agents record that they were alive. Outside the repo — an agent that committed the
    evidence of its own concurrency would be a fine joke and a broken test."""
    ledger = repo.path.parent / "ledger"
    ledger.write_text("")
    monkeypatch.setenv(LEDGER_ENV, str(ledger))
    return ledger


async def test_all_currently_eligible_sub_issues_run_concurrently(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four independent sub-issues.

    `peak == 4` is the scheduler's promise now: every currently eligible sibling is dispatched.

    It also settles a question no other test can. The merge lock is **never held while an agent
    runs**: if it were, these four could not have overlapped at all.
    """
    repo.write_graph({"01": [], "02": [], "03": [], "04": []})
    ledger = with_ledger(monkeypatch, repo)

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SLOW),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert sorted(report.landed) == ["01", "02", "03", "04"]
    assert peak_concurrency(ledger) == 4


async def test_a_fast_sub_issue_lands_without_waiting_for_a_slower_sibling(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """**No wave barrier.** Waves are an artifact of dependencies, not of merging.

    01 is slow, 02 is fast, neither blocks the other. 02 lands *first* — ahead of a sibling that
    was dispatched before it and sorts before it. A generation-at-a-time scheduler cannot do that,
    and it is the difference between a fast sub-issue landing in three minutes and it waiting for
    the slowest thing in its wave.
    """
    repo.write_graph({"01": [], "02": []})
    spec = behaviour_spec(Behaviour.SUCCEED, {"01": Behaviour.SLOW})

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert report.landed == (SubIssueId("02"), SubIssueId("01"))


async def test_concurrent_sub_issues_serialize_into_a_linear_history(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Three at once, landing one at a time. Each rebases onto whatever the last one landed, so the
    integration branch grows by fast-forward and there is not a merge commit in it."""
    repo.write_graph({"01": [], "02": [], "03": []})

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert report.clean
    assert repo.commit_count("integration") == 5  # initial + the graph + three sub-issues
    assert repo.git("log", "--merges", "--oneline", "integration") == ""
    assert repo.run_suite() is True
    assert all((repo.path / f"feature_{id}.py").exists() for id in ("01", "02", "03"))


async def test_a_rebase_conflict_does_not_stall_the_queue_for_its_siblings(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """01 and 02 both rewrite the same line; 03 is minding its own business.

    One of the two wins the lock and lands. The other's rebase conflicts — which is
    `integration-failed`, a signal about how the work was cut, **not** a transient hiccup. The lock
    is released, nothing is retried, and 03 lands regardless.
    """
    repo.write_graph({"01": [], "02": [], "03": []})
    spec = behaviour_spec(Behaviour.CONFLICT, {"03": Behaviour.SUCCEED})

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert SubIssueId("03") in report.landed
    losers = [id for id, o in report.failed.items() if o is Outcome.INTEGRATION_FAILED]
    assert len(losers) == 1, "exactly one of the two conflicting sub-issues should have landed"
    assert len(report.landed) == 2  # the winner, and 03

    # No retry: the loser opened exactly one session.
    opened = [
        line for line in (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()
        if f'"{losers[0]}"' in line
        and '"actor": "implementer"' in line
        and "session-started" in line
    ]
    assert len(opened) == 1

    assert repo.git("log", "--merges", "--oneline", "integration") == ""
    assert repo.run_suite() is True


async def test_a_sub_issue_is_dispatched_the_moment_its_blockers_land(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diamond. 04 waits for 02 and 03; 01 is slow and blocks nothing.

    04 must run *while 01 is still going* — its blockers are satisfied, and 01 is nothing to do
    with it. A scheduler that drained a generation before starting the next would make 04 wait for
    a sub-issue it does not depend on.
    """
    repo.write_graph({"01": [], "02": [], "03": [], "04": ["02", "03"]})
    spec = behaviour_spec(Behaviour.SUCCEED, {"01": Behaviour.SLOW})
    ledger = with_ledger(monkeypatch, repo)

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert report.clean
    assert report.landed[-1] == SubIssueId("01")  # the slow one finished last, blocking nobody

    marks = ledger.read_text().split()
    assert "+04" in marks
    assert marks.index("+04") < marks.index("-01"), "04 waited for a sub-issue it does not depend on"
