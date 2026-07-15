"""**The Editor closes the loop.**

A failed sub-issue goes to an Editor, comes back with a verdict, and — on `revise` — **restarts
clean** against a rewritten brief. This is the file where the harness stops being a runner and
starts being a harness.

The distinction it exists to defend: **a cycle is not a retry.** A retry runs the same actor against
the same brief and hopes for a better sample. A cycle runs a *different actor* over the failure,
which rewrites the brief, and only then runs the Implementer again — against something that has
changed. If any test here would still pass with the Editor deleted and the session simply re-run,
it is testing the wrong thing.

Driven by a stub Editor with scripted verdicts, deliberately: the loop is proven before any model is
involved, and #09 swaps in the real one without touching a line of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ralph.harness import (
    Actor,
    CycleLedger,
    Destination,
    Outcome,
    SessionTelemetry,
    Verdict,
    route,
)
from ralph.issues import Brief, Findings, SubIssueId, SubIssueState
from ralph.cli import render, run
from ralph.mergequeue import MergeQueue
from ralph.ports import Budget, Editor
from ralph.scheduler import RunReport, Scheduler
from tests.builders import graph_of, impasse, telemetry, verdict
from tests.fakes import (
    FakeEditor,
    FakeGit,
    FakeImplementer,
    FakeIssueStore,
    FakeRunLog,
    FakeTestRunner,
)
from tests.testbed import LEDGER_ENV, Behaviour, StandInAgent, TargetRepo

REPO = Path("/repo")
ONE = SubIssueId("01")

IMPASSE = telemetry(commits=0, impasse_report=impasse())
UNDECLARED_IMPASSE = telemetry(commits=0)
SUCCESS = telemetry(commits=1)


def store_of(*ids: str) -> FakeIssueStore:
    return FakeIssueStore(
        graph=graph_of({id: [] for id in ids}),
        states={SubIssueId(id): SubIssueState.READY for id in ids},
    )


async def run_with(
    store: FakeIssueStore,
    implementer: FakeImplementer,
    editor: Editor | None,
    git: FakeGit | None = None,
    log: FakeRunLog | None = None,
) -> RunReport:
    git, log = git or FakeGit(head="integration"), log or FakeRunLog()
    runner = FakeTestRunner()
    return await Scheduler(
        repo=REPO,
        git=git,
        store=store,
        run_log=log,
        runner=runner,
        implementer=implementer,
        editor=editor,
        merge_queue=MergeQueue(git=git, runner=runner, integration="integration"),
        integration="integration",
        budget=Budget(),
        concurrency=2,
    ).run()


# --- what reaches the Editor at all ---------------------------------------------------------------


def test_exactly_two_outcomes_reach_the_editor() -> None:
    """Asserted over the **full** `Outcome` enum, via `route` — not over a list of examples.

    A test that checked two outcomes go to the Editor would still pass if a third were quietly
    added to the routing table. The interesting half of this claim is the half about what does *not*
    reach it: `ceiling-exceeded` and `infra-failed` are unhelpable by an Editor, and a harness that
    sent them there would burn a cycle and a model on a question with no answer.
    """
    to_the_editor = {
        outcome
        for outcome in Outcome
        if route(Actor.IMPLEMENTER, outcome) is Destination.EDITOR
    }
    assert to_the_editor == {Outcome.IMPASSE, Outcome.INTEGRATION_FAILED}


@pytest.mark.parametrize(
    ("session", "expected"),
    [(IMPASSE, Outcome.IMPASSE), (UNDECLARED_IMPASSE, Outcome.IMPASSE)],
)
async def test_an_implementer_failure_is_handed_to_the_editor(
    session: SessionTelemetry, expected: Outcome
) -> None:
    """The Editor is given the brief, the findings, and the failure report — the model's story
    checked against the harness's facts. Not the transcript, and not a summary the harness wrote."""
    store = store_of("01")
    store.contents[ONE] = (Brief(body="the original brief"), Findings(body="what we knew"))
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.PLANNING_DEFECT))])

    await run_with(store, FakeImplementer(scripted=[session]), editor)

    assert len(editor.calls) == 1
    context, failure, must_be_terminal = editor.calls[0]
    assert context.brief.body == "the original brief"
    assert context.findings.body == "what we knew"
    assert failure.outcome is expected
    assert must_be_terminal is False  # first cycle of three


async def test_integration_failed_spends_a_cycle_exactly_like_an_impasse() -> None:
    """It was green in isolation and it will not integrate — a fact the Implementer could not have
    observed about itself. So it goes to the Editor like any other failure, and it **costs** the same
    as any other failure. A sub-issue that keeps failing to integrate escalates rather than looping
    forever against a moving integration branch."""
    store = store_of("01")
    git = FakeGit(head="integration", ff_refuses={"ralph/01"})
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.REVISE))])

    report = await run_with(store, FakeImplementer(scripted=[SUCCESS]), editor, git=git)

    # Three Implementer sessions, three Editor sessions: the cap bit, and it bit on an outcome that
    # only the merge queue can raise.
    assert [f.outcome for _, f, _ in editor.calls] == [Outcome.INTEGRATION_FAILED] * 3
    assert report.failed == {ONE: Outcome.INTEGRATION_FAILED}


# --- revise: the work is discarded, the brief is rewritten ---------------------------------------


async def test_a_revise_discards_the_work_and_restarts_clean_against_the_revised_brief() -> None:
    """The whole ticket in one test.

    The Implementer declares an impasse. The Editor rewrites the brief. The second Implementer
    session gets a **fresh worktree cut from the integration branch** and the **revised** brief —
    not its own abandoned diff, and not the brief it already failed against.
    """
    store = store_of("01")
    store.contents[ONE] = (Brief(body="the original brief"), Findings(body="what we knew"))
    implementer = FakeImplementer(scripted=[IMPASSE, SUCCESS])
    editor = FakeEditor(
        scripted=[
            (
                telemetry(commits=0),
                verdict(Verdict.REVISE, brief="build it with the API that exists"),
            )
        ]
    )
    git = FakeGit(head="integration")

    report = await run_with(store, implementer, editor, git=git)

    assert report.landed == (ONE,)  # it landed on the second cycle

    # The first worktree was destroyed — branch and all — not carried forward and not quarantined.
    assert git.discarded == ["ralph/01"]
    assert git.moved == []

    # And the second was cut fresh from the integration branch, not from the wreckage.
    assert [wt.base for wt in git.worktrees] == ["integration", "integration"]

    # The second session read the Editor's brief. This is the assertion that separates a cycle from
    # a retry: a retry would show "the original brief" twice.
    assert [context.brief.body for context in implementer.calls] == [
        "the original brief",
        "build it with the API that exists",
    ]


async def test_knowledge_survives_only_through_the_findings() -> None:
    """The diff is discarded. The transcript is discarded. What the Editor chose to write into the
    findings is **all** the next session gets — and that choice is the Editor's judgment, unmandated.

    The findings are kept out of the brief on purpose: they are a channel for adding information
    *without* lowering the bar. An Editor that could only help by editing the acceptance criteria
    would have no way to say "the retry logic swallows the error" except by making the sub-issue
    easier.
    """
    store = store_of("01")
    store.contents[ONE] = (Brief(body="the bar"), Findings(body=""))
    implementer = FakeImplementer(scripted=[IMPASSE, SUCCESS])
    editor = FakeEditor(
        scripted=[
            (
                telemetry(commits=0),
                verdict(
                    Verdict.REVISE,
                    brief="the bar",  # unchanged: the bar is not lowered
                    findings="the client's retry logic swallows the expected error",
                ),
            )
        ]
    )

    await run_with(store, implementer, editor)

    second = implementer.calls[1]
    assert second.brief.body == "the bar"  # the bar did not move
    assert second.findings.body == "the client's retry logic swallows the expected error"


async def test_a_revision_may_change_the_findings_without_the_brief() -> None:
    """Brief and findings round-trip as **separate** fields. An Editor that returns no findings has
    left them alone, and the harness must carry the old ones forward rather than blanking them."""
    store = store_of("01")
    store.contents[ONE] = (Brief(body="the bar"), Findings(body="what the last session learned"))
    implementer = FakeImplementer(scripted=[IMPASSE, SUCCESS])
    editor = FakeEditor(
        scripted=[(telemetry(commits=0), verdict(Verdict.REVISE, brief="a clearer bar"))]
    )

    await run_with(store, implementer, editor)

    _, brief, findings = store.revisions[0]
    assert brief.body == "a clearer bar"  # changed
    assert findings.body == "what the last session learned"  # untouched, not blanked


# --- the terminal verdicts ------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", [Verdict.PLANNING_DEFECT, Verdict.INCONCLUSIVE])
async def test_a_terminal_verdict_quarantines_immediately(terminal: Verdict) -> None:
    """`planning-defect`: the brief cannot be satisfied as written, and rewriting it is a Planner's
    call. `inconclusive`: the Editor could not tell. Neither spends a second cycle — another
    Implementer session would be a coin flip we have already paid for once.
    """
    store = store_of("01")
    implementer = FakeImplementer(scripted=[IMPASSE])
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(terminal))])
    git = FakeGit(head="integration")

    report = await run_with(store, implementer, editor, git=git)

    assert report.failed == {ONE: Outcome.IMPASSE}  # escalates on what actually failed
    assert len(implementer.calls) == 1  # no second session
    assert store.revisions == []  # and no revision was stored
    assert git.moved == [("ralph/01", REPO / ".worktrees" / "failed" / "01")]  # evidence preserved
    assert git.discarded == []


# --- the cycle cap --------------------------------------------------------------------------------


async def test_no_fourth_implementer_session_is_ever_dispatched() -> None:
    """An Editor that says `revise` forever. It gets three Implementer sessions and not one more.

    This is the test that stops the harness becoming an infinite loop with a language model in it.
    """
    store = store_of("01")
    implementer = FakeImplementer(scripted=[IMPASSE])  # never succeeds
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.REVISE))])  # never gives up

    report = await run_with(store, implementer, editor)

    assert len(implementer.calls) == CycleLedger.MAX_CYCLES == 3
    assert len(editor.calls) == 3
    assert report.failed == {ONE: Outcome.IMPASSE}
    assert report.notification.escalations[0].report.cycles == 3


async def test_the_scheduler_and_only_the_scheduler_rejects_a_third_cycle_revise() -> None:
    """The adapters *tell* the model it is the final cycle; the scheduler **rejects** a `revise`
    that comes back anyway.

    Both halves are asserted, because only together do they mean anything. A harness that told the
    Editor and then obeyed it would be trusting a model to enforce the harness's own cap — and a
    rule enforced by asking nicely is not a rule.
    """
    store = store_of("01")
    implementer = FakeImplementer(scripted=[IMPASSE])
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.REVISE))])

    report = await run_with(store, implementer, editor)

    # Told: false, false, then true on the cycle that has no successor.
    assert [must_be_terminal for _, _, must_be_terminal in editor.calls] == [False, False, True]

    # Refused: the third `revise` was returned, and ignored. Two revisions were stored, not three —
    # the rejected one never became a brief, because nothing would ever have read it.
    assert len(store.revisions) == 2
    assert report.failed == {ONE: Outcome.IMPASSE}
    assert not report.clean


async def test_ceiling_and_infra_failures_spend_no_cycle() -> None:
    """A cycle is an Implementer session **plus the Editor session that follows it**. No Editor is
    involved in either of these, so there is no cycle to spend — and no Editor is asked about a
    failure it cannot help with."""
    store = store_of("01", "02")
    implementer = FakeImplementer(
        scripted=[telemetry(killed="ceiling", exit_code=137, commits=0)]
    )
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.REVISE))])

    report = await run_with(store, implementer, editor)

    assert editor.calls == []  # it was never asked
    assert len(implementer.calls) == 2  # one each for 01 and 02 — neither was ever re-run
    assert report.failed == {
        ONE: Outcome.CEILING_EXCEEDED,
        SubIssueId("02"): Outcome.CEILING_EXCEEDED,
    }
    assert report.notification.escalations[0].report.cycles == 1


# --- when the Editor itself fails -----------------------------------------------------------------


async def test_an_editor_that_returns_no_verdict_escalates_as_infra_failed() -> None:
    """The Editor's product is its verdict. No verdict, no session — and the escalation is the
    **Editor's** failure, not the Implementer's.

    The sub-issue's own impasse is no longer the interesting fact. That the harness cannot
    adjudicate it is: a human reading `impasse` here would go and rewrite a brief, when what actually
    needs fixing is the Editor.
    """
    store = store_of("01")
    editor = FakeEditor(scripted=[(telemetry(commits=0), None)])
    git = FakeGit(head="integration")

    report = await run_with(store, FakeImplementer(scripted=[IMPASSE]), editor, git=git)

    assert report.failed == {ONE: Outcome.INFRA_FAILED}
    assert git.moved == [("ralph/01", REPO / ".worktrees" / "failed" / "01")]
    assert git.discarded == []  # the evidence is kept, not thrown away


async def test_a_ceiling_killed_editor_pages_the_human_like_any_other_actor() -> None:
    """The Editor is bounded exactly like an Implementer: same Budget, same telemetry. It can come
    back `ceiling-exceeded`, and when it does there is nobody left to adjudicate the adjudicator."""
    store = store_of("01")
    editor = FakeEditor(scripted=[(telemetry(killed="ceiling", commits=0), None)])

    report = await run_with(store, FakeImplementer(scripted=[IMPASSE]), editor)

    assert report.failed == {ONE: Outcome.CEILING_EXCEEDED}


async def test_a_killed_editor_still_spends_its_cycle() -> None:
    """Spent when the Editor half *begins*, not when it succeeds.

    Otherwise an Editor that reliably times out would buy its sub-issue an unbounded number of
    Implementer sessions — the cap would be enforced only along the paths that were working anyway.
    """
    store = store_of("01")
    editor = FakeEditor(scripted=[(telemetry(commits=0), None)])  # no verdict, ever

    report = await run_with(store, FakeImplementer(scripted=[IMPASSE]), editor)

    assert len(editor.calls) == 1  # it failed, and that was the end of it
    assert report.notification.escalations[0].report.cycles == 1


# --- the run log ----------------------------------------------------------------------------------


async def test_the_scheduler_writes_every_event_and_the_adapters_write_none() -> None:
    """`Editor.adjudicate` and `Implementer.run` take no `RunLog`, by design — they are given a
    brief and a worktree and they return what they found. Everything that *happened* is the
    scheduler's to record, and the log below is the proof that it recorded all of it.

    Every line names its actor. Without that, a cycle's two `session-finished` lines are ambiguous,
    and the authoritative record of the run cannot say whether the Implementer or the Editor was the
    thing that broke.
    """
    store = store_of("01")
    implementer = FakeImplementer(scripted=[IMPASSE, SUCCESS])
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.REVISE))])
    log = FakeRunLog()

    await run_with(store, implementer, editor, log=log)

    assert [(e.sub_issue, e.actor.value, e.kind.value, e.details.value) for e in log.events()] == [
        ("01", "implementer", "session-started", "in-progress"),
        ("01", "implementer", "session-finished", "impasse"),
        ("01", "editor", "session-started", "in-progress"),
        ("01", "editor", "session-finished", "success"),  # the Editor's session
        ("01", "editor", "verdict-recorded", "revise"),
        ("01", "implementer", "session-started", "in-progress"),  # against a new brief
        ("01", "implementer", "session-finished", "success"),
        ("01", "implementer", "sub-issue-closed", "landed"),
    ]


# --- and the run still drains ---------------------------------------------------------------------


async def test_a_sub_issue_in_its_second_cycle_does_not_stall_its_siblings() -> None:
    """Cycles are per-sub-issue and they hold their slot in the semaphore, but they do not hold up
    the graph. 02 is independent of 01 and lands while 01 is still arguing with the Editor."""
    store = store_of("01", "02")
    implementer = FakeImplementer(scripted=[IMPASSE, SUCCESS, SUCCESS, SUCCESS])
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.REVISE))])

    report = await run_with(store, implementer, editor)

    assert sorted(report.landed) == [ONE, SubIssueId("02")]
    assert report.clean


# --- against a real repository --------------------------------------------------------------------


async def test_a_revise_really_discards_the_work_and_the_sub_issue_really_lands(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real git, real worktrees, a real agent subprocess, a real suite — and a stub Editor, which is
    the only thing here that is not real, because no Editor adapter exists until #09.

    The agent commits a partial attempt, declares an impasse, and is restarted. **The partial attempt
    must not survive.** In the fakes it is enough to assert `discard_worktree` was called; here the
    claim is checked against the thing that actually matters — the history that landed. A harness
    that merely *moved* the failed worktree aside, or that re-cut the branch without deleting it,
    passes every fake-level test in this file and fails this one.
    """
    repo.write_graph({"01": []})
    monkeypatch.setenv(
        "RALPH_AGENT_CMD",
        f"{sys.executable} {agent.script} {Behaviour.IMPASSE_ONCE.value} {{sub_issue}}",
    )
    editor = FakeEditor(
        scripted=[
            (
                telemetry(commits=0),
                verdict(Verdict.REVISE, brief="use the API that exists", findings="it is `add()`"),
            )
        ]
    )

    report = await run(repo.path, None, editor=editor)

    assert report.landed == (ONE,)
    assert report.clean
    assert len(editor.calls) == 1  # one cycle: it landed on the second attempt

    # The second attempt's work is on the integration branch, and the suite is green on it.
    assert (repo.path / "feature_01.py").exists()
    assert repo.run_suite() is True

    # The first attempt's is nowhere: not in the tree, not in the history, not on a branch.
    assert not (repo.path / "partial_01.py").exists()
    assert "wip(01)" not in repo.git("log", "--oneline", "--all")

    # And the Editor really was handed the impasse the agent really emitted.
    _, failure, _ = editor.calls[0]
    assert failure.outcome is Outcome.IMPASSE
    assert failure.claim is not None
    assert failure.claim.unsatisfiable_criterion == "the second acceptance criterion of 01"


async def test_the_planners_original_survives_a_real_run(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the run, revision 0 is still on disk and still says what the Planner said — even though
    the live `.md` file has had its `Status:` line rewritten underneath it."""
    repo.write_graph({"01": []})
    monkeypatch.setenv(
        "RALPH_AGENT_CMD",
        f"{sys.executable} {agent.script} {Behaviour.IMPASSE_ONCE.value} {{sub_issue}}",
    )
    original = (repo.issues_dir / "01-sub.md").read_text()
    editor = FakeEditor(
        scripted=[(telemetry(commits=0), verdict(Verdict.REVISE, brief="a clearer bar"))]
    )

    await run(repo.path, None, editor=editor)

    revisions = repo.issues_dir / "revisions" / "01"
    assert (revisions / "0-brief.md").read_text() == original
    assert (revisions / "1-brief.md").read_text() == "a clearer bar"

    # The live file moved on — `Status: landed` — which is exactly why the snapshot has to exist.
    assert "Status: landed" in (repo.issues_dir / "01-sub.md").read_text()


async def test_a_real_run_dispatches_no_fourth_implementer_session(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that always fails, and an Editor that always says try again. Three sessions, then a
    human — proven against a real repo, where "a session" means a real subprocess really ran."""
    repo.write_graph({"01": []})
    monkeypatch.setenv(
        "RALPH_AGENT_CMD",
        f"{sys.executable} {agent.script} {Behaviour.IMPASSE.value} {{sub_issue}}",
    )
    ledger = repo.path.parent / "ledger"
    ledger.write_text("")
    monkeypatch.setenv(LEDGER_ENV, str(ledger))
    editor = FakeEditor(scripted=[(telemetry(commits=0), verdict(Verdict.REVISE))])

    report = await run(repo.path, None, editor=editor)

    # The agent itself counted three starts. Not the harness's word for it.
    assert ledger.read_text().count("+01") == CycleLedger.MAX_CYCLES == 3
    assert report.failed == {ONE: Outcome.IMPASSE}
    assert "Status: needs-human" in (repo.issues_dir / "01-sub.md").read_text()

    # The evidence from the *final* cycle is what a human is pointed at.
    assert (repo.path / ".worktrees" / "failed" / "01").is_dir()

    # And the notification says the loop was exhausted, rather than reading like a first failure.
    assert "(cycle 3 of 3)" in render(report.notification)
