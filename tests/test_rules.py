"""The rules: classify, route, eligibility, the cycle cap.

Every test here is pure — no subprocess, no git, no model. That is the point of `rules/`: the
highest-value logic in the system is also the cheapest to test.
"""

from __future__ import annotations

import pytest

from ralph.domain import (
    Actor,
    CycleLedger,
    Destination,
    EditorVerdict,
    Outcome,
    SubIssueId,
    SubIssueState,
    Verdict,
    classify_editor,
    classify_implementer,
    eligible,
    never_eligible,
    route,
)
from tests.builders import graph_of, impasse, suite, telemetry

# --- classify_implementer: one test per row of the taxonomy -------------------------------------


def test_a_green_session_that_committed_is_success() -> None:
    assert classify_implementer(telemetry(), suite(green=True)) is Outcome.SUCCESS


def test_the_impasse_sentinel_is_an_impasse() -> None:
    t = telemetry(exit_code=1, impasse_report=impasse())
    assert classify_implementer(t, suite(green=False)) is Outcome.IMPASSE


def test_zero_commits_is_silent_red_never_success() -> None:
    """`no commits - skipping` was the prototype's worst lie. A session that produced nothing
    did not succeed, whatever the suite says about the tree it never touched."""
    assert classify_implementer(telemetry(commits=0), suite(green=True)) is Outcome.SILENT_RED


def test_a_session_that_committed_but_left_the_suite_red_is_silent_red() -> None:
    """The suite result, not the exit code. The model exited 0 and is wrong about it."""
    t = telemetry(exit_code=0, commits=3)
    assert classify_implementer(t, suite(green=False)) is Outcome.SILENT_RED


def test_a_ceiling_kill_is_ceiling_exceeded_not_infra_failed() -> None:
    """Both exit non-zero. Conflating them retries the session straight back out of the smart
    zone."""
    t = telemetry(exit_code=137, killed="ceiling", peak_context_tokens=121_000, commits=0)
    assert classify_implementer(t, suite(green=False)) is Outcome.CEILING_EXCEEDED


def test_a_wall_clock_kill_is_infra_failed() -> None:
    t = telemetry(exit_code=124, killed="wall-clock", commits=0)
    assert classify_implementer(t, suite(green=False)) is Outcome.INFRA_FAILED


def test_exit_124_is_infra_failed_even_if_the_harness_did_not_do_the_killing() -> None:
    """`timeout(1)` may have got there first. Same failure, same route."""
    t = telemetry(exit_code=124, killed=None, commits=0)
    assert classify_implementer(t, suite(green=False)) is Outcome.INFRA_FAILED


def test_the_ceiling_outranks_the_impasse_sentinel() -> None:
    """A killed session's transcript may still contain a sentinel it wrote before it died. What
    happened to it is that it left the smart zone."""
    t = telemetry(killed="ceiling", impasse_report=impasse())
    assert classify_implementer(t, suite(green=False)) is Outcome.CEILING_EXCEEDED


def test_the_impasse_sentinel_outranks_zero_commits() -> None:
    """Declaring an impasse and committing nothing is the *expected* shape of an impasse — it is
    not a silent red."""
    t = telemetry(commits=0, impasse_report=impasse())
    assert classify_implementer(t, suite(green=False)) is Outcome.IMPASSE


def test_a_non_zero_exit_without_a_sentinel_is_not_an_impasse() -> None:
    """The process died. Do not assume the model gave up."""
    t = telemetry(exit_code=1, commits=0, impasse_report=None)
    assert classify_implementer(t, suite(green=False)) is not Outcome.IMPASSE


def test_the_implementer_never_classifies_integration_failed() -> None:
    """It does not classify a session at all. Only the merge queue raises it."""
    for t, s in [
        (telemetry(), suite(green=True)),
        (telemetry(commits=0), suite(green=False)),
        (telemetry(killed="ceiling"), suite(green=False)),
        (telemetry(impasse_report=impasse()), suite(green=False)),
    ]:
        assert classify_implementer(t, s) is not Outcome.INTEGRATION_FAILED


# --- classify_editor ----------------------------------------------------------------------------


def _verdict(v: Verdict = Verdict.INCONCLUSIVE) -> EditorVerdict:
    return EditorVerdict(
        verdict=v,
        revised_brief=None,
        revised_findings=None,
        rationale="because",
    )


def test_editor_success_is_a_verdict_returned() -> None:
    assert classify_editor(telemetry(commits=0), _verdict()) is Outcome.SUCCESS


def test_an_editor_that_returned_no_verdict_is_infra_failed() -> None:
    """Whatever it exited with. An Editor's product is its verdict; no verdict, no session."""
    assert classify_editor(telemetry(exit_code=0), None) is Outcome.INFRA_FAILED


def test_a_ceiling_killed_editor_is_ceiling_exceeded() -> None:
    t = telemetry(killed="ceiling", peak_context_tokens=125_000)
    assert classify_editor(t, None) is Outcome.CEILING_EXCEEDED


def test_a_wall_clocked_editor_is_infra_failed() -> None:
    assert classify_editor(telemetry(exit_code=124, killed="wall-clock"), None) is (
        Outcome.INFRA_FAILED
    )


def test_classify_editor_cannot_return_impasse_or_silent_red_for_any_input() -> None:
    """Structural, not incidental: an Editor cannot declare itself stuck, and it commits nothing
    that could go silently red. Assert over the input space, not one example."""
    forbidden = {Outcome.IMPASSE, Outcome.SILENT_RED, Outcome.INTEGRATION_FAILED}
    telemetries = [
        telemetry(),
        telemetry(exit_code=1, commits=0),
        telemetry(exit_code=124, killed="wall-clock"),
        telemetry(killed="ceiling"),
        telemetry(commits=7, impasse_report=impasse()),  # an Editor that committed and whinged
    ]
    verdicts: list[EditorVerdict | None] = [None, *(_verdict(v) for v in Verdict)]

    for t in telemetries:
        for v in verdicts:
            assert classify_editor(t, v) not in forbidden


# --- route --------------------------------------------------------------------------------------


def test_an_implementers_success_goes_to_the_merge_queue() -> None:
    assert route(Actor.IMPLEMENTER, Outcome.SUCCESS) is Destination.MERGE_QUEUE


def test_an_editors_success_is_a_verdict_to_act_on() -> None:
    assert route(Actor.EDITOR, Outcome.SUCCESS) is Destination.ACT_ON_VERDICT


@pytest.mark.parametrize(
    "outcome", [Outcome.IMPASSE, Outcome.SILENT_RED, Outcome.INTEGRATION_FAILED]
)
def test_the_three_diagnosable_failures_go_to_the_editor(outcome: Outcome) -> None:
    assert route(Actor.IMPLEMENTER, outcome) is Destination.EDITOR


@pytest.mark.parametrize("outcome", [Outcome.CEILING_EXCEEDED, Outcome.INFRA_FAILED])
def test_the_two_outcomes_the_editor_never_sees_go_to_the_human_from_either_actor(
    outcome: Outcome,
) -> None:
    """Exhaustive over Actor on purpose: an Editor that exceeded the ceiling must not be handed
    to an Editor."""
    for actor in Actor:
        assert route(actor, outcome) is Destination.HUMAN
        assert route(actor, outcome) is not Destination.EDITOR


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
