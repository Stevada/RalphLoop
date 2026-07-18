"""A sub-issue's state.

There is no state for a sub-issue whose upstream escalated. It stays unstarted with its
`blocked by` edge intact, which is already distinct from `needs-human` — "I failed" and "I never
got a turn" are distinguishable without inventing a state for the second. Nothing propagates a
skip through the graph; `never_eligible` derives it at report time instead.

There is no `not-started` either: `ready` *is* the Planner's authorisation to run, and a sub-issue
it has not authorised does not belong in the graph yet.
"""

from __future__ import annotations

from enum import StrEnum


class SubIssueState(StrEnum):
    READY = "ready"  # stored: the Planner authorised this sub-issue to run
    IN_PROGRESS = "in-progress"
    LANDED = "landed"  # terminal: fast-forwarded into the integration branch
    NEEDS_HUMAN = "needs-human"  # quarantine: planning-defect, inconclusive, impasse, or infra
