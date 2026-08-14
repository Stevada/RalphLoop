"""The rules: classify, route, eligibility, the cycle cap, the failure report, the notification.

Every test here is pure — no subprocess, no git, no model. That is the point of `rules/`: the
highest-value logic in the system is also the cheapest to test.
"""

from __future__ import annotations

import pytest

from ralph.harness import (
    Actor,
    CycleLedger,
    Destination,
    EditorVerdict,
    FailureReport,
    Outcome,
    Verdict,
    classify_editor,
    classify_implementer,
    eligible,
    failure_report,
    never_eligible,
    route,
)
from ralph.issues import SubIssueId, SubIssueState
from ralph.notification import ATTENTION_ORDER, notify
from tests.builders import graph_of, impasse, suite, telemetry, verdict

# --- classify_implementer: one test per row of the taxonomy -------------------------------------


def test_a_committed_impasse_free_session_is_success() -> None:
    assert classify_implementer(telemetry()) is Outcome.SUCCESS


def test_the_impasse_sentinel_is_an_impasse() -> None:
    t = telemetry(exit_code=1, impasse_report=impasse())
    assert classify_implementer(t) is Outcome.IMPASSE


def test_zero_commits_is_an_impasse_never_success() -> None:
    """`no commits - skipping` was the prototype's worst lie. A session that produced nothing
    did not succeed — it is an impasse the model never declared."""
    assert classify_implementer(telemetry(commits=0)) is Outcome.IMPASSE


def test_a_committed_session_is_success_without_a_post_session_suite() -> None:
    """A committed session with no sentinel reaches the merge gate. The scheduler no longer
    re-runs the suite to turn it into an undeclared impasse."""
    t = telemetry(exit_code=0, commits=3)
    assert classify_implementer(t) is Outcome.SUCCESS


def test_a_wall_clock_kill_is_infra_failed() -> None:
    t = telemetry(exit_code=124, killed="wall-clock", commits=0)
    assert classify_implementer(t) is Outcome.INFRA_FAILED


def test_exit_124_is_infra_failed_even_if_the_harness_did_not_do_the_killing() -> None:
    """`timeout(1)` may have got there first. Same failure, same route."""
    t = telemetry(exit_code=124, killed=None, commits=0)
    assert classify_implementer(t) is Outcome.INFRA_FAILED


def test_a_declared_impasse_that_committed_nothing_is_an_impasse() -> None:
    """Declaring an impasse and committing nothing is the *expected* shape of a declared impasse.
    The sentinel and the empty tree agree; the model stood behind a claim, which the report keeps."""
    t = telemetry(commits=0, impasse_report=impasse())
    assert classify_implementer(t) is Outcome.IMPASSE
    assert failure_report(Outcome.IMPASSE, t).claim is not None


def test_a_non_zero_exit_without_a_sentinel_is_an_undeclared_impasse() -> None:
    """The process died without declaring anything. It is still an impasse — it did not deliver —
    but an undeclared one: the harness must not manufacture a claim the model never made."""
    t = telemetry(exit_code=1, commits=0, impasse_report=None)
    assert classify_implementer(t) is Outcome.IMPASSE
    assert failure_report(Outcome.IMPASSE, t).claim is None


def test_the_implementer_never_classifies_integration_failed() -> None:
    """It does not classify a session at all. Only the merge gate raises it."""
    for t in [
        telemetry(),
        telemetry(commits=0),
        telemetry(killed="wall-clock"),
        telemetry(impasse_report=impasse()),
    ]:
        assert classify_implementer(t) is not Outcome.INTEGRATION_FAILED


def test_neither_classifier_can_produce_the_retired_ceiling_outcome() -> None:
    produced = {
        classify_implementer(telemetry()).value,
        classify_implementer(telemetry(commits=0)).value,
        classify_implementer(telemetry(killed="wall-clock")).value,
        classify_editor(telemetry(), verdict()).value,
        classify_editor(telemetry(exit_code=0), None).value,
        classify_editor(telemetry(killed="wall-clock"), None).value,
    }

    assert "ceiling-exceeded" not in produced


# --- classify_editor ----------------------------------------------------------------------------


def test_editor_success_is_a_verdict_returned() -> None:
    assert classify_editor(telemetry(commits=0), verdict()) is Outcome.SUCCESS


def test_an_editor_that_returned_no_verdict_is_infra_failed() -> None:
    """Whatever it exited with. An Editor's product is its verdict; no verdict, no session."""
    assert classify_editor(telemetry(exit_code=0), None) is Outcome.INFRA_FAILED


def test_a_wall_clocked_editor_is_infra_failed() -> None:
    assert classify_editor(telemetry(exit_code=124, killed="wall-clock"), None) is (
        Outcome.INFRA_FAILED
    )


def test_classify_editor_cannot_return_impasse_for_any_input() -> None:
    """Structural, not incidental: an Editor has no spec of its own to fail to deliver, declared
    or not. Assert over the input space, not one example."""
    forbidden = {Outcome.IMPASSE, Outcome.INTEGRATION_FAILED}
    telemetries = [
        telemetry(),
        telemetry(exit_code=1, commits=0),
        telemetry(exit_code=124, killed="wall-clock"),
        telemetry(commits=7, impasse_report=impasse()),  # an Editor that committed and whinged
    ]
    verdicts: list[EditorVerdict | None] = [None, *(verdict(v) for v in Verdict)]

    for t in telemetries:
        for v in verdicts:
            assert classify_editor(t, v) not in forbidden


# --- route --------------------------------------------------------------------------------------


def test_an_implementers_success_goes_to_the_merge_gate() -> None:
    assert route(Actor.IMPLEMENTER, Outcome.SUCCESS) is Destination.MERGE_GATE


def test_an_editors_success_is_a_verdict_to_act_on() -> None:
    assert route(Actor.EDITOR, Outcome.SUCCESS) is Destination.ACT_ON_VERDICT


def test_an_integrators_success_continues_the_landing_it_is_inside() -> None:
    """Not back to the merge gate: it never released the merge lock, so a reconciled
    worktree carries on to the suite gate rather than making a second trip."""
    assert route(Actor.INTEGRATOR, Outcome.SUCCESS) is Destination.SUITE_GATE


@pytest.mark.parametrize("outcome", [Outcome.IMPASSE, Outcome.INTEGRATION_FAILED])
def test_the_diagnosable_failures_go_to_the_editor(outcome: Outcome) -> None:
    assert route(Actor.IMPLEMENTER, outcome) is Destination.EDITOR


def test_an_integrators_integration_failure_goes_to_a_human_not_the_editor() -> None:
    """The same outcome, the same word, a different answer — because the actor changes what it
    means. Raised against an Implementer it says two trees disagree and an Editor should look.
    Raised against the Integrator it says the actor *sent* to reconcile them could not, and no spec
    was ever wrong, so there is nothing for an Editor to rewrite.
    """
    assert route(Actor.INTEGRATOR, Outcome.INTEGRATION_FAILED) is Destination.HUMAN
    assert route(Actor.IMPLEMENTER, Outcome.INTEGRATION_FAILED) is Destination.EDITOR


def test_infra_failures_go_to_the_human_from_any_actor() -> None:
    for actor in Actor:
        assert route(actor, Outcome.INFRA_FAILED) is Destination.HUMAN
        assert route(actor, Outcome.INFRA_FAILED) is not Destination.EDITOR


def test_no_route_returns_a_retry_and_route_is_total() -> None:
    """`Destination` having no retry member is asserted in the contract; this asserts the other
    half — that no row reaches for one. Every (Actor, Outcome) pair has an answer, and none of
    them raises."""
    for actor in Actor:
        for outcome in Outcome:
            assert route(actor, outcome) in set(Destination)


# --- eligibility --------------------------------------------------------------------------------

_01, _02, _03 = SubIssueId("01"), SubIssueId("02"), SubIssueId("03")


def test_a_sub_issue_is_eligible_only_when_every_blocker_has_landed() -> None:
    graph = graph_of({"01": [], "02": [], "03": ["01", "02"]})
    states = {
        _01: SubIssueState.LANDED,
        _02: SubIssueState.IN_PROGRESS,
        _03: SubIssueState.READY,
    }
    assert eligible(graph, states) == frozenset()

    states[_02] = SubIssueState.LANDED
    assert eligible(graph, states) == frozenset({_03})


def test_a_blocker_in_needs_human_never_makes_its_dependent_eligible() -> None:
    graph = graph_of({"01": [], "02": ["01"]})
    states = {_01: SubIssueState.NEEDS_HUMAN, _02: SubIssueState.READY}
    assert eligible(graph, states) == frozenset()


def test_only_ready_sub_issues_are_eligible() -> None:
    """An in-progress sub-issue is not dispatched twice; a landed one is not dispatched again."""
    graph = graph_of({"01": [], "02": [], "03": []})
    states = {
        _01: SubIssueState.READY,
        _02: SubIssueState.IN_PROGRESS,
        _03: SubIssueState.LANDED,
    }
    assert eligible(graph, states) == frozenset({_01})


def test_never_eligible_finds_a_sub_issue_transitively_behind_a_quarantined_one() -> None:
    graph = graph_of({"01": [], "02": ["01"], "03": ["02"]})
    states = {
        _01: SubIssueState.NEEDS_HUMAN,
        _02: SubIssueState.READY,
        _03: SubIssueState.READY,
    }
    # 03's only blocker is 02, which is merely READY — the quarantine is a step further back.
    assert never_eligible(graph, states) == frozenset({_02, _03})


def test_never_eligible_marks_nothing() -> None:
    """It reports. It does not write a state — there is no `skipped`, and nothing propagates
    through the graph."""
    graph = graph_of({"01": [], "02": ["01"]})
    states = {_01: SubIssueState.NEEDS_HUMAN, _02: SubIssueState.READY}

    never_eligible(graph, states)

    assert states == {_01: SubIssueState.NEEDS_HUMAN, _02: SubIssueState.READY}


def test_a_sub_issue_with_a_landed_blocker_is_not_never_eligible() -> None:
    graph = graph_of({"01": [], "02": ["01"]})
    states = {_01: SubIssueState.LANDED, _02: SubIssueState.READY}
    assert never_eligible(graph, states) == frozenset()


# --- the cycle cap ------------------------------------------------------------------------------


def test_the_ledger_permits_at_most_three_cycles() -> None:
    ledger = CycleLedger()
    assert [ledger.spend(_01) for _ in range(3)] == [1, 2, 3]
    assert ledger.exhausted(_01)
    with pytest.raises(ValueError, match="no fourth"):
        ledger.spend(_01)


def test_must_be_terminal_is_true_on_the_third_cycle_and_false_before_it() -> None:
    ledger = CycleLedger()
    assert not ledger.must_be_terminal(_01)  # no cycle spent yet
    ledger.spend(_01)
    assert not ledger.must_be_terminal(_01)
    ledger.spend(_01)
    assert not ledger.must_be_terminal(_01)
    ledger.spend(_01)
    assert ledger.must_be_terminal(_01)


def test_the_ledger_counts_each_sub_issue_separately() -> None:
    ledger = CycleLedger()
    ledger.spend(_01)
    ledger.spend(_01)
    ledger.spend(_01)
    assert ledger.exhausted(_01)
    assert not ledger.exhausted(_02)
    assert ledger.spend(_02) == 1


def test_an_untouched_sub_issue_has_spent_nothing() -> None:
    ledger = CycleLedger()
    assert not ledger.exhausted(_01)
    assert not ledger.must_be_terminal(_01)


# --- failure_report: the model's story, and the story the harness will not invent ----------------


def test_the_claim_is_only_ever_what_the_model_actually_emitted() -> None:
    report = failure_report(Outcome.IMPASSE, telemetry(impasse_report=impasse()))

    assert report.claim is not None
    assert report.claim.unsatisfiable_criterion == "the third acceptance criterion"
    assert report.suite is None


def test_a_session_that_emitted_no_sentinel_gets_no_claim() -> None:
    """**The harness never fabricates an impasse report.** A killed session authored none;
    synthesising one from a partial transcript would be the least honest artifact the system could
    produce — a story with no author, handed to the Editor as though a model stood behind it.
    """
    report = failure_report(Outcome.INFRA_FAILED, telemetry(killed="wall-clock", commits=0))

    assert report.claim is None
    assert report.telemetry.killed == "wall-clock"  # what is left is what the harness saw itself
    assert report.suite is None


def test_a_non_integration_report_carries_no_harness_suite_result() -> None:
    """The Editor must reproduce non-integration failures in the worktree; the scheduler no longer
    hands it a post-session suite result."""
    report = failure_report(
        Outcome.IMPASSE,
        telemetry(session_output="All tests pass!", impasse_report=impasse()),
    )

    assert report.claim is not None
    assert "All tests pass" in report.telemetry.session_output
    assert report.suite is None


def test_an_integration_failure_report_carries_the_merge_gate_suite() -> None:
    report = failure_report(
        Outcome.INTEGRATION_FAILED,
        telemetry(),
        suite(green=False, output="1 failed"),
        "suite-red",
    )

    assert report.suite is not None
    assert not report.suite.green
    assert report.suite.output == "1 failed"
    assert report.integration_detail == "suite-red"


# --- notify: one notification, and which one to open first ---------------------------------------


def escalate(outcome: Outcome) -> FailureReport:
    return failure_report(outcome, telemetry())


def test_the_notification_counts_what_each_failure_stranded() -> None:
    """Transitive: the sub-issue behind the sub-issue behind the quarantined one is just as stuck,
    and nothing in between was marked to say so."""
    graph = graph_of({"01": [], "02": ["01"], "03": ["02"], "04": []})
    states = {
        SubIssueId("01"): SubIssueState.NEEDS_HUMAN,
        SubIssueId("02"): SubIssueState.READY,
        SubIssueId("03"): SubIssueState.READY,
        SubIssueId("04"): SubIssueState.LANDED,
    }

    n = notify(
        graph,
        states,
        [SubIssueId("04")],
        {SubIssueId("01"): escalate(Outcome.IMPASSE)},
        {},
    )

    assert n.landed == (SubIssueId("04"),)
    assert n.escalations[0].stranded == (SubIssueId("02"), SubIssueId("03"))


def test_the_outcome_decides_what_kind_of_ten_minutes_you_are_about_to_spend() -> None:
    """Infra first — it means the harness itself broke, and nothing else this run says is
    trustworthy. Then the outcomes that send you to a spec, then the ones that send you to a diff.
    """
    assert ATTENTION_ORDER[Outcome.INFRA_FAILED] < ATTENTION_ORDER[Outcome.IMPASSE]
    assert ATTENTION_ORDER[Outcome.IMPASSE] < ATTENTION_ORDER[Outcome.INTEGRATION_FAILED]
    assert Outcome.SUCCESS not in ATTENTION_ORDER  # a success does not escalate


def test_a_declared_impasse_opens_before_an_undeclared_one() -> None:
    """Same outcome, different morning. A declared impasse — the model explained a criterion it
    could not meet — is read before an integration failure; an undeclared one, the suite catching a
    session that thought it was done, is read after. The split is taken from the report's `claim`,
    not from a second outcome."""
    declared = failure_report(Outcome.IMPASSE, telemetry(impasse_report=impasse()))
    undeclared = failure_report(Outcome.IMPASSE, telemetry(commits=0))
    integration = failure_report(Outcome.INTEGRATION_FAILED, telemetry(), suite(green=False))

    graph = graph_of({"01": [], "02": [], "03": []})
    states = {i: SubIssueState.NEEDS_HUMAN for i in map(SubIssueId, ("01", "02", "03"))}

    n = notify(
        graph,
        states,
        [],
        {
            SubIssueId("01"): undeclared,
            SubIssueId("02"): declared,
            SubIssueId("03"): integration,
        },
        {},
    )

    assert [e.sub_issue for e in n.escalations] == [
        SubIssueId("02"),  # declared impasse, opened first
        SubIssueId("03"),  # integration-failed, between them
        SubIssueId("01"),  # undeclared impasse, opened last
    ]


def test_the_ranking_puts_the_worse_kind_first_even_when_it_strands_less() -> None:
    graph = graph_of({"01": [], "02": [], "03": ["02"]})
    states = {
        SubIssueId("01"): SubIssueState.NEEDS_HUMAN,  # infra-failed, stranding nobody
        SubIssueId("02"): SubIssueState.NEEDS_HUMAN,  # undeclared impasse, stranding 03
        SubIssueId("03"): SubIssueState.READY,
    }

    n = notify(
        graph,
        states,
        [],
        {
            SubIssueId("02"): escalate(Outcome.IMPASSE),
            SubIssueId("01"): escalate(Outcome.INFRA_FAILED),
        },
        {},
    )

    assert [e.sub_issue for e in n.escalations] == [SubIssueId("01"), SubIssueId("02")]


def test_blast_radius_breaks_the_tie_between_two_failures_of_the_same_kind() -> None:
    """The outcome tells you what kind of ten minutes you are about to spend; the blast radius tells
    you which of two identical failures to open first."""
    graph = graph_of({"01": [], "02": [], "03": ["02"], "04": ["02"]})
    states = {
        SubIssueId("01"): SubIssueState.NEEDS_HUMAN,
        SubIssueId("02"): SubIssueState.NEEDS_HUMAN,
        SubIssueId("03"): SubIssueState.READY,
        SubIssueId("04"): SubIssueState.READY,
    }

    n = notify(
        graph,
        states,
        [],
        {
            SubIssueId("01"): escalate(Outcome.IMPASSE),
            SubIssueId("02"): escalate(Outcome.IMPASSE),
        },
        {},
    )

    assert [e.sub_issue for e in n.escalations] == [SubIssueId("02"), SubIssueId("01")]


def test_a_success_that_reached_the_notification_is_a_defect_and_says_so() -> None:
    """It cannot happen — `route` sends a success to the merge gate. If it ever does, the scheduler
    quarantined something it had classified as fine, and silence would be the worst answer."""
    graph = graph_of({"01": []})
    states = {SubIssueId("01"): SubIssueState.NEEDS_HUMAN}

    with pytest.raises(ValueError, match="not a failure"):
        notify(graph, states, [], {SubIssueId("01"): escalate(Outcome.SUCCESS)}, {})
