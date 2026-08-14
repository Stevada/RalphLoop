"""**Many land, in parallel, serialized by the merge lock.**

Sub-issues run in parallel but land one at a time. That is the whole trick, and every test here is
the real thing: real concurrent subprocesses, a real merge lock, real merges of a branch that is
moving under them.

The hard part of testing parallelism is proving it *happened*. A run that is secretly sequential
passes every correctness assertion you can write — the work still lands, the branch still ends up
correct. So the stand-in agent keeps a ledger of when it was alive, and the tests read the
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
    StandInIntegrator,
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


async def test_sequential_runs_one_sub_issue_at_a_time_and_still_lands_them_all(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same four independent sub-issues, with `--sequential`.

    `peak == 1` is the whole claim: nothing else can tell a serialized run from a parallel one that
    happened to interleave politely. They all still land, in graph order, because narrowing the
    dispatch does not touch the merge gate.
    """
    repo.write_graph({"01": [], "02": [], "03": [], "04": []})
    ledger = with_ledger(monkeypatch, repo)

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SLOW),
        editor=unengaged_editor(),
        options=make_options(),
        sequential=True,
    )

    assert report.landed == (
        SubIssueId("01"),
        SubIssueId("02"),
        SubIssueId("03"),
        SubIssueId("04"),
    )
    assert peak_concurrency(ledger) == 1


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


async def test_concurrent_sub_issues_serialize_into_fast_forwards(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Three at once, landing one at a time. Each merges in whatever the last one landed, runs its
    suite on that result, and the integration branch then grows by fast-forward alone."""
    repo.write_graph({"01": [], "02": [], "03": []})

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert report.clean
    catch_ups = repo.git("log", "--merges", "--format=%s", "integration").splitlines()
    assert all(m.startswith("Merge branch 'integration' into ralph/") for m in catch_ups)
    # initial + the graph + three sub-issues, plus a catch-up merge per worktree that had to
    # integrate a sibling that won the lock first.
    assert repo.commit_count("integration") == 5 + len(catch_ups)
    assert repo.run_suite() is True
    assert all((repo.path / f"feature_{id}.py").exists() for id in ("01", "02", "03"))


async def test_a_merge_conflict_is_reconciled_inside_the_queue_and_both_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """01 and 02 both rewrite the same line; 03 is minding its own business.

    One of the two wins the lock and lands. The other conflicts — and instead of failing, an
    **Integrator** is dispatched into the conflicted worktree without the lock ever being released.
    All three land, no Editor is ever engaged, and no Implementer session is opened twice.
    """
    repo.write_graph({"01": [], "02": [], "03": []})
    spec = behaviour_spec(Behaviour.CONFLICT, {"03": Behaviour.SUCCEED})
    integrator = StandInIntegrator()

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=terminal_editor(),
        integrator=integrator,
        options=make_options(),
    )

    assert report.clean
    assert sorted(report.landed) == ["01", "02", "03"]
    assert len(integrator.calls) == 1, "only the sub-issue that lost the lock needed reconciling"
    assert repo.run_suite() is True

    # No retry: every sub-issue opened exactly one Implementer session.
    log = (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()
    opened = [
        line for line in log if '"actor": "implementer"' in line and "session-started" in line
    ]
    assert len(opened) == 3
    # And the reconciliation is on the record, under its own actor.
    assert any('"actor": "integrator"' in line for line in log)


async def test_an_unreconciled_conflict_pages_a_human_and_its_siblings_still_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The Integrator ran and left the merge open. That sub-issue is quarantined — no Editor, because
    no spec was wrong — and everything unaffected still lands."""
    repo.write_graph({"01": [], "02": [], "03": []})
    spec = behaviour_spec(Behaviour.CONFLICT, {"03": Behaviour.SUCCEED})

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, spec),
        editor=terminal_editor(),
        integrator=StandInIntegrator(resolves=False),
        options=make_options(),
    )

    assert SubIssueId("03") in report.landed
    assert len(report.landed) == 2  # the winner of the lock, and 03
    losers = [id for id, o in report.failed.items() if o is Outcome.INTEGRATION_FAILED]
    assert len(losers) == 1, "exactly one of the two conflicting sub-issues should have landed"

    # No Editor was engaged for it: a textual conflict is not evidence that a spec is wrong.
    log = (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()
    assert not any('"actor": "editor"' in line and f'"{losers[0]}"' in line for line in log)
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
