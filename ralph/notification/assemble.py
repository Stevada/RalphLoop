"""Assemble the end-of-run notification from failures and issue state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ralph.harness import FailureReport, Outcome, never_eligible
from ralph.issues.graph import IssueGraph, SubIssueId
from ralph.issues.state import SubIssueState
from ralph.notification.model import Escalation, Notification

ATTENTION_ORDER: Mapping[Outcome, int] = {
    # The harness or the environment broke. Nothing else this run says is trustworthy until you
    # know why, so it does not matter what else is in the list.
    Outcome.INFRA_FAILED: 0,
    # The sub-issue was too big to reason about inside the smart zone. Re-cut it — a Planner's job,
    # and no amount of reading the diff will tell you that.
    Outcome.CEILING_EXCEEDED: 1,
    # The model declared an impasse and explained itself: an acceptance criterion it believes cannot
    # be satisfied. If it is right, you are rewriting a brief, not a function.
    Outcome.IMPASSE: 2,
    # Green alone, red together. The defect is in how the work was cut across sub-issues, so the
    # diff to read is the *pair* of them.
    Outcome.INTEGRATION_FAILED: 3,
}

# An undeclared impasse: no brief to reconsider, just a diff to read, so it is opened last.
_UNDECLARED_IMPASSE = 4


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
    return (_attention(e.report), -len(e.stranded), e.sub_issue)


def _attention(report: FailureReport) -> int:
    """How urgent this failure is to open, lower first. An undeclared impasse (no `claim`) is read
    after everything else; every other failure ranks by its outcome alone."""
    if report.outcome is Outcome.IMPASSE and report.claim is None:
        return _UNDECLARED_IMPASSE
    if report.outcome not in ATTENTION_ORDER:
        # SUCCESS is the only other member, and a success does not escalate. Reaching here means
        # the scheduler quarantined something it had classified as fine.
        raise ValueError(
            f"escalated with outcome {report.outcome!r}, which is not a failure"
        )
    return ATTENTION_ORDER[report.outcome]
