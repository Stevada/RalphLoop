"""Quarantine-and-drain, assembled into the single thing a human reads afterwards.

Two decisions live here, and both are judgment rather than bookkeeping:

**What each failure cost.** Nothing propagates through the graph when a sub-issue escalates — there
is no `skipped` state and no marking. Its dependents simply never satisfy `eligible`, because their
blocker never reaches LANDED. That is what keeps the graph honest, but it also means the damage is
*invisible* unless someone goes looking. So we look, here, at the end: `stranded` is that lookup.

**Which one to open first.** The outcome says what kind of ten minutes you are about to spend; the
blast radius breaks ties between two failures of the same kind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ralph.domain.model.failure import FailureReport
from ralph.domain.model.graph import IssueGraph, SubIssueId
from ralph.domain.model.notification import Escalation, Notification
from ralph.domain.model.session import Outcome
from ralph.domain.model.state import SubIssueState
from ralph.domain.rules.eligibility import never_eligible

ATTENTION_ORDER: Mapping[Outcome, int] = {
    # The harness or the environment broke. Nothing else this run says is trustworthy until you
    # know why, so it does not matter what else is in the list.
    Outcome.INFRA_FAILED: 0,
    # The sub-issue was too big to reason about inside the smart zone. Re-cut it — a Planner's job,
    # and no amount of reading the diff will tell you that.
    Outcome.CEILING_EXCEEDED: 1,
    # The model says an acceptance criterion cannot be satisfied, and explained itself. If it is
    # right, you are rewriting a brief, not a function.
    Outcome.IMPASSE: 2,
    # Green alone, red together. The defect is in how the work was cut across sub-issues, so the
    # diff to read is the *pair* of them.
    Outcome.INTEGRATION_FAILED: 3,
    # It committed nothing, or it committed a red suite and said nothing. Read the diff.
    Outcome.SILENT_RED: 4,
}


def notify(
    graph: IssueGraph,
    states: Mapping[SubIssueId, SubIssueState],
    landed: Sequence[SubIssueId],
    failures: Mapping[SubIssueId, FailureReport],
) -> Notification:
    """One notification, at the end. What landed, what failed and why, and what to open first."""
    stranded = never_eligible(graph, states)

    escalations = [
        Escalation(
            sub_issue=id,
            outcome=report.outcome,
            report=report,
            # Transitive on purpose: the sub-issue behind the sub-issue behind this one is just as
            # stuck, and nothing in between was marked to say so.
            stranded=tuple(
                sorted(s for s in stranded if id in graph.transitively_blocked_by(s))
            ),
        )
        for id, report in failures.items()
    ]
    return Notification(
        landed=tuple(landed),
        escalations=tuple(sorted(escalations, key=_open_this_one_first)),
    )


def _open_this_one_first(e: Escalation) -> tuple[int, int, str]:
    if e.outcome not in ATTENTION_ORDER:
        # SUCCESS is the only other member, and a success does not escalate. Reaching here means
        # the scheduler quarantined something it had classified as fine.
        raise ValueError(f"{e.sub_issue} escalated with outcome {e.outcome!r}, which is not a failure")
    return (ATTENTION_ORDER[e.outcome], -len(e.stranded), e.sub_issue)
