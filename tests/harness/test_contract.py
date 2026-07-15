"""The contract itself: the shape ten other sub-issues are written against.

Field names are part of the contract. A rename here is a breakage for everything downstream, so
these assertions are deliberately literal.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from pathlib import Path

import pytest

from ralph.harness import (
    Actor,
    CycleLedger,
    Destination,
    EditorVerdict,
    ImpasseReport,
    Outcome,
    SessionTelemetry,
    SuiteResult,
    Verdict,
)
from ralph.issues import Brief, Findings, IssueGraph, SubIssue, SubIssueState
from ralph.issues.store import IssueStore
from ralph.ports import (
    Budget,
    ContextSource,
    Editor,
    Git,
    Implementer,
    RunLog,
    SessionContext,
    TestRunner,
    Worktree,
)
from tests.builders import graph_of
from tests.fakes import (
    FakeContextSource,
    FakeEditor,
    FakeGit,
    FakeImplementer,
    FakeIssueStore,
    FakeRunLog,
    FakeTestRunner,
)

FROZEN = [SubIssue, IssueGraph, Brief, Findings, SessionTelemetry, SuiteResult, EditorVerdict]
ENUMS = [Actor, Outcome, SubIssueState, Destination, Verdict]


@pytest.mark.parametrize("cls", FROZEN, ids=lambda c: c.__name__)
def test_domain_dataclasses_are_frozen_with_slots(cls: type) -> None:
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen, f"{cls.__name__} is not frozen"
    assert dataclasses.fields(cls) is not None
    assert "__slots__" in cls.__dict__, f"{cls.__name__} was not built with slots=True"


@pytest.mark.parametrize("enum", ENUMS, ids=lambda e: e.__name__)
def test_the_state_types_are_str_enums(enum: type) -> None:
    assert issubclass(enum, StrEnum)


def test_the_outcome_taxonomy_is_exactly_four_failures_and_one_success() -> None:
    assert {o.value for o in Outcome} == {
        "success",
        "impasse",
        "integration-failed",
        "ceiling-exceeded",
        "infra-failed",
    }


def test_there_is_no_retry_destination() -> None:
    """There is no retry anywhere in this system."""
    assert {d.value for d in Destination} == {"merge-queue", "act-on-verdict", "editor", "human"}
    assert not any("retry" in d.value for d in Destination)


def test_landed_is_terminal_and_done_is_not_a_sub_issue_state() -> None:
    assert SubIssueState.LANDED.value == "landed"
    assert "done" not in {s.value for s in SubIssueState}


def test_the_suite_result_field_is_green() -> None:
    """Never `verified`, `trusted`, or `passing`. The day someone writes `if suite.verified:` is
    the day the blast-radius distinction starts to erode."""
    names = {f.name for f in dataclasses.fields(SuiteResult)}
    assert "green" in names
    assert names.isdisjoint({"verified", "trusted", "passing"})


def test_the_ceiling_is_on_context_and_defaults_to_the_smart_zone() -> None:
    assert Budget().max_context_tokens == 120_000
    assert Budget().wall_clock_s == 1800.0
    assert not any("retry" in f.name for f in dataclasses.fields(Budget))


def test_session_telemetry_carries_context_and_consumption_separately() -> None:
    names = {f.name for f in dataclasses.fields(SessionTelemetry)}
    assert {"peak_context_tokens", "consumed_tokens"} <= names


def test_revise_is_the_only_non_terminal_verdict() -> None:
    assert not Verdict.REVISE.is_terminal
    assert Verdict.PLANNING_DEFECT.is_terminal
    assert Verdict.INCONCLUSIVE.is_terminal


def test_the_cycle_cap_is_three() -> None:
    assert CycleLedger.MAX_CYCLES == 3


# --- the fakes structurally satisfy the ports -------------------------------------------------
# The annotations are the real assertion: mypy --strict checks them. The runtime isinstance is a
# second, weaker net for anyone running the suite without a typecheck.


def test_every_port_has_a_fake_that_satisfies_it() -> None:
    implementer: Implementer = FakeImplementer()
    editor: Editor = FakeEditor()
    store: IssueStore = FakeIssueStore(graph=graph_of({"01": []}))
    log: RunLog = FakeRunLog()
    runner: TestRunner = FakeTestRunner()
    git: Git = FakeGit()
    context: ContextSource = FakeContextSource()

    assert isinstance(implementer, Implementer)
    assert isinstance(editor, Editor)
    assert isinstance(store, IssueStore)
    assert isinstance(log, RunLog)
    assert isinstance(runner, TestRunner)
    assert isinstance(git, Git)
    assert isinstance(context, ContextSource)


def test_the_worktree_knows_where_it_came_from() -> None:
    git = FakeGit()
    wt = git.add_worktree("sub-01", Path("/tmp/wt"), "integration")
    assert isinstance(wt, Worktree)
    assert wt.base == "integration"


def test_session_context_groups_the_shared_actor_inputs() -> None:
    wt = Worktree(path=Path("/tmp/wt"), branch="ralph/01", base="integration")
    context = SessionContext(
        brief=Brief(body="build it"),
        findings=Findings(body="facts"),
        worktree=wt,
        budget=Budget(wall_clock_s=1.0),
    )

    assert [f.name for f in dataclasses.fields(SessionContext)] == [
        "brief",
        "findings",
        "worktree",
        "budget",
    ]
    assert context.worktree is wt
    assert context.budget.wall_clock_s == 1.0


def test_an_impasse_report_is_the_models_story_not_the_harnesss_facts() -> None:
    """The claim and the corroboration are separate types on purpose — the harness checks one
    against the other, and their disagreeing is itself a signal."""
    claim = {f.name for f in dataclasses.fields(ImpasseReport)}
    facts = {f.name for f in dataclasses.fields(SessionTelemetry)}
    assert claim.isdisjoint(facts)
