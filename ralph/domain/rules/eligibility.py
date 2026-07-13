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
    raise NotImplementedError


def never_eligible(
    graph: IssueGraph, states: Mapping[SubIssueId, SubIssueState]
) -> frozenset[SubIssueId]:
    """Report-time only. A sub-issue still READY at run end whose blockers never landed never got
    a turn. This is the drain half of quarantine-and-drain: observed, never propagated."""
    raise NotImplementedError
