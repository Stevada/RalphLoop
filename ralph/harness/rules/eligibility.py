"""Who may run, and who never got a turn.

Eligibility is **derived, never stored**. That is what makes quarantine-and-drain work without a
`skipped` state: when a sub-issue escalates, nothing propagates through the graph — its dependents
simply never satisfy the rule below, because their blocker never reaches LANDED.
"""

from __future__ import annotations

from collections.abc import Mapping

from ralph.issues.graph import IssueGraph, SubIssueId
from ralph.issues.state import SubIssueState


def eligible(
    graph: IssueGraph, states: Mapping[SubIssueId, SubIssueState]
) -> frozenset[SubIssueId]:
    """Every READY sub-issue whose blockers have all LANDED."""
    return frozenset(
        id
        for id in graph.sub_issues
        if states[id] is SubIssueState.READY
        and all(states[b] is SubIssueState.LANDED for b in graph.blockers_of(id))
    )


def build_order(
    graph: IssueGraph, states: Mapping[SubIssueId, SubIssueState]
) -> tuple[tuple[SubIssueId, ...], ...]:
    """The waves a run would dispatch in, if every sub-issue landed.

    Derived by asking `eligible` the same question the scheduler asks, over and over, rather than by
    a second topological sort — which would be a second opinion about the graph, free to disagree
    with the one the run actually acts on. A dry run whose plan is not the run's plan is worse than
    no dry run at all.

    The waves are a *plan*, not a promise: the scheduler has no wave barrier and will start a
    sub-issue the moment its blockers land, not when its slowest sibling finishes. What the waves
    are honest about is the **order**, which is the only thing the graph actually determines.
    """
    remaining = dict(states)
    waves: list[tuple[SubIssueId, ...]] = []
    while wave := tuple(sorted(eligible(graph, remaining))):
        waves.append(wave)
        for id in wave:
            remaining[id] = SubIssueState.LANDED
    return tuple(waves)


def never_eligible(
    graph: IssueGraph, states: Mapping[SubIssueId, SubIssueState]
) -> frozenset[SubIssueId]:
    """The drain half of quarantine-and-drain: a READY sub-issue standing behind one that
    escalated. Its blocker will never reach LANDED, so `eligible` will never return it, and no
    state is ever written to say so — this is observed, never propagated.

    Transitive on purpose: the sub-issue behind the sub-issue behind the quarantined one is just
    as stuck, and nothing in between was marked.
    """
    return frozenset(
        id
        for id in graph.sub_issues
        if states[id] is SubIssueState.READY
        and any(
            states[b] is SubIssueState.NEEDS_HUMAN for b in graph.transitively_blocked_by(id)
        )
    )
