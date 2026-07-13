"""Who may run, and who never got a turn.

Eligibility is **derived, never stored**. That is what makes quarantine-and-drain work without a
`skipped` state: when a sub-issue escalates, nothing propagates through the graph — its dependents
simply never satisfy the rule below, because their blocker never reaches LANDED.
"""

from __future__ import annotations

from collections.abc import Mapping

from ralph.domain.model.graph import IssueGraph, SubIssueId
from ralph.domain.model.state import SubIssueState


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
