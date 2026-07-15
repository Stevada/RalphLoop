"""**A failure is classified and quarantined, and the run survives it honestly.**

The run does not stop. *A system that pages you the instant the first thing goes wrong trains you
to ignore it.* So: the failure is classified into the taxonomy, the sub-issue is marked
`needs-human`, its worktree is preserved as evidence, its dependents never get a turn, every
unaffected sub-issue still lands, and **one** notification comes out at the end.

Nothing propagates through the graph. There is no `skipped` state anywhere in this file, because
there is none anywhere in the system: a dependent of a quarantined sub-issue is not marked, it is
simply never eligible. That is the thing being tested.
"""

from __future__ import annotations

import json
import sys

import pytest

from ralph.cli import render, run
from ralph.domain import Outcome
from ralph.issues import SubIssueId
from ralph.ports import Budget
from tests.testbed import Behaviour, StandInAgent, TargetRepo, behaviour_spec


def script_the_agent(
    monkeypatch: pytest.MonkeyPatch, agent: StandInAgent, spec: Behaviour | str
) -> None:
    monkeypatch.setenv("RALPH_AGENT_CMD", f"{sys.executable} {agent.script} {spec} {{sub_issue}}")


def story(repo: TargetRepo) -> list[tuple[str, str, str]]:
    lines = (repo.path / ".scratch" / "run.jsonl").read_text().splitlines()
    return [(e["sub_issue"], e["kind"], e["details"]) for e in (json.loads(x) for x in lines)]


async def test_a_failure_quarantines_and_the_run_drains_around_it(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """01 declares an impasse. 03 stands behind it. 02 and 04 have nothing to do with either.

    The whole shape of quarantine-and-drain in one run: 01 escalates, 03 never gets a turn, and 02
    and 04 land anyway — because a run that stopped at the first failure would waste every
    independent sub-issue in the graph.
    """
    repo.write_graph({"01": [], "02": [], "03": ["01"], "04": ["02"]})
    script_the_agent(monkeypatch, agent, behaviour_spec(Behaviour.SUCCEED, {"01": Behaviour.IMPASSE}))

    report = await run(repo.path, None, concurrency=3)

    assert sorted(report.landed) == ["02", "04"]  # every unaffected sub-issue still landed
    assert report.failed == {SubIssueId("01"): Outcome.IMPASSE}

    # 03 never ran. No branch, no worktree, no session — and nothing was written to say so.
    assert not repo.branch_exists("ralph/03")
    assert "03" not in {id for id, _, _ in story(repo)}

    # It carries no failure state. It is `ready`, with its `blocked by` edge intact, exactly as the
    # Planner left it. There is no `skipped`, and nothing propagated.
    dependent = (repo.issues_dir / "03-sub.md").read_text()
    assert "Status: ready" in dependent
    assert "## Blocked by" in dependent and "#01" in dependent

    assert "Status: needs-human" in (repo.issues_dir / "01-sub.md").read_text()


async def test_the_quarantined_worktree_is_preserved_as_evidence(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worktree is what a human reads to decide whether the model was right. Preserved, and
    moved somewhere they will find it — `.worktrees/failed/` is where the notification says it is."""
    repo.write_graph({"01": []})
    script_the_agent(monkeypatch, agent, Behaviour.RED_SUITE)

    await run(repo.path, None)

    failed = repo.path / ".worktrees" / "failed" / "01"
    assert failed.is_dir()
    assert (failed / "test_broken_01.py").exists()  # the wreckage, exactly as the agent left it
    assert not (repo.path / ".worktrees" / "active" / "01").exists()


async def test_a_killed_session_yields_a_report_built_from_harness_facts_only(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The harness never fabricates an impasse report.**

    A session killed on the wall clock authored nothing. Synthesising a plausible story from its
    partial transcript would be the least honest artifact this system could produce — a narrative
    with no author, handed to the Editor as though a model had stood behind it. So `claim` is None,
    and what remains is what the harness observed for itself.
    """
    repo.write_graph({"01": []})
    script_the_agent(monkeypatch, agent, Behaviour.HANG)

    report = await run(repo.path, None, Budget(wall_clock_s=1.0))

    escalation = report.notification.escalations[0]
    assert escalation.outcome is Outcome.INFRA_FAILED
    assert escalation.report.claim is None  # it said nothing, so we say nothing on its behalf
    assert escalation.report.telemetry.killed == "wall-clock"
    assert escalation.report.telemetry.commits == 0


async def test_a_report_carries_both_the_models_claim_and_the_harnesss_facts(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent says, in as many words, "All tests pass." The suite is red.

    The report carries **both**, because the harness collects its facts independently of the model's
    narration — and the two disagreeing is itself the signal. A report that quietly dropped the
    claim would be hiding the most interesting thing in it.
    """
    repo.write_graph({"01": []})
    script_the_agent(monkeypatch, agent, Behaviour.RED_SUITE)

    report = await run(repo.path, None)

    escalation = report.notification.escalations[0]
    assert escalation.outcome is Outcome.IMPASSE
    assert "All tests pass" in escalation.report.telemetry.session_output  # the model's story
    assert not escalation.report.suite.green  # the harness's fact
    assert escalation.report.telemetry.commits == 1  # it really did commit; it was just wrong


async def test_zero_commits_is_an_impasse_never_a_benign_skip(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It exits 0 and says the code was already fine. The prototype called that a skip and moved on
    — which is how a graph of ten sub-issues finishes in four minutes having done nothing at all."""
    repo.write_graph({"01": []})
    script_the_agent(monkeypatch, agent, Behaviour.COMMIT_NOTHING)

    report = await run(repo.path, None)

    assert report.failed == {SubIssueId("01"): Outcome.IMPASSE}
    assert report.notification.escalations[0].report.telemetry.commits == 0


async def test_nothing_is_ever_retried(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `infra-failed` session results in exactly **one** session for that sub-issue, and it
    quarantines. Not once more, not with backoff. Retrying burns the budget, delays the
    notification, and produces a second failure report nobody wanted."""
    repo.write_graph({"01": [], "02": []})
    script_the_agent(monkeypatch, agent, behaviour_spec(Behaviour.SUCCEED, {"01": Behaviour.HANG}))

    report = await run(repo.path, None, Budget(wall_clock_s=1.0), concurrency=2)

    assert report.failed == {SubIssueId("01"): Outcome.INFRA_FAILED}
    opened = [(id, kind) for id, kind, _ in story(repo) if kind == "session-started"]
    assert opened.count((SubIssueId("01"), "session-started")) == 1
    assert SubIssueId("02") in report.landed  # and the run drained around it


async def test_one_notification_says_which_to_open_first(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bar: *if you cannot tell from it alone whether to spend your first ten minutes reading a
    diff or rewriting a PRD, the notification has failed.*

    Two failures. 01 declared an impasse and is holding up two sub-issues — that is a brief to
    rewrite. 02 committed a red suite and is holding up nothing — that is a diff to read. The
    impasse comes first, and the notification says why, in the model's own words.
    """
    repo.write_graph({"01": [], "02": [], "03": ["01"], "04": ["03"]})
    script_the_agent(
        monkeypatch,
        agent,
        behaviour_spec(Behaviour.SUCCEED, {"01": Behaviour.IMPASSE, "02": Behaviour.RED_SUITE}),
    )

    report = await run(repo.path, None, concurrency=2)
    n = report.notification

    assert [e.sub_issue for e in n.escalations] == ["01", "02"]
    assert n.escalations[0].outcome is Outcome.IMPASSE
    assert n.escalations[0].stranded == ("03", "04")  # transitive: 04 is behind 03 is behind 01
    assert n.escalations[1].stranded == ()

    text = render(n)
    assert "the second acceptance criterion of 01" in text  # the criterion it says it cannot meet
    assert "an API that exists" in text  # and what would satisfy it
    assert "holding up: 03, 04" in text
    assert ".worktrees/failed/01" in text  # where the evidence is
