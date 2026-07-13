"""The graph's constructor invariants. These are real behaviour, not stubs: the whole point of a
frozen IssueGraph is that an invalid one cannot be constructed in the first place."""

from __future__ import annotations

import dataclasses

import pytest

from ralph.domain import GraphError, IssueGraph, SubIssue, SubIssueId
from tests.builders import graph_of


def test_rejects_a_two_node_cycle() -> None:
    with pytest.raises(GraphError, match="cycle"):
        graph_of({"01": ["02"], "02": ["01"]})


def test_rejects_a_self_loop() -> None:
    with pytest.raises(GraphError, match="cycle"):
        graph_of({"01": ["01"]})


def test_rejects_a_dangling_edge() -> None:
    with pytest.raises(GraphError, match="not a sub-issue"):
        graph_of({"01": ["nope"]})


def test_rejects_a_mislabelled_key() -> None:
    sub = SubIssue(id=SubIssueId("01"), title="t", blocked_by=frozenset())
    with pytest.raises(GraphError, match="but its id is"):
        IssueGraph(sub_issues={SubIssueId("02"): sub})


def test_accepts_a_diamond() -> None:
    g = graph_of({"01": [], "02": ["01"], "03": ["01"], "04": ["02", "03"]})
    assert g.blockers_of(SubIssueId("04")) == {SubIssueId("02"), SubIssueId("03")}


def test_transitively_blocked_by_closes_over_the_graph() -> None:
    g = graph_of({"01": [], "02": ["01"], "03": ["01"], "04": ["02", "03"]})
    assert g.transitively_blocked_by(SubIssueId("04")) == {
        SubIssueId("01"),
        SubIssueId("02"),
        SubIssueId("03"),
    }
    assert g.transitively_blocked_by(SubIssueId("01")) == frozenset()


def test_a_sub_issue_is_frozen() -> None:
    sub = SubIssue(id=SubIssueId("01"), title="t", blocked_by=frozenset())
    with pytest.raises(dataclasses.FrozenInstanceError):
        sub.title = "renamed"  # type: ignore[misc]


def test_a_graph_is_frozen_and_its_mapping_cannot_be_written_through() -> None:
    g = graph_of({"01": []})
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.sub_issues = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        g.sub_issues[SubIssueId("02")] = g.sub_issues[SubIssueId("01")]  # type: ignore[index]


def test_the_graph_copies_the_mapping_it_was_given() -> None:
    """Mutating the caller's dict after construction must not reach into the graph."""
    source = {
        SubIssueId("01"): SubIssue(id=SubIssueId("01"), title="t", blocked_by=frozenset()),
    }
    g = IssueGraph(sub_issues=source)
    source.clear()
    assert set(g.sub_issues) == {SubIssueId("01")}
