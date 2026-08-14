"""Assemble the end-of-run notification from failures and issue state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ralph.harness import FailureReport, Outcome, never_eligible, total
from ralph.issues import SessionConsumption
from ralph.issues.graph import IssueGraph, SubIssueId
from ralph.issues.state import SubIssueState
from ralph.notification.model import Escalation, Notification, SubIssueConsumption

ATTENTION_ORDER: Mapping[Outcome, int] = {
    # The harness or the environment broke. Nothing else this run says is trustworthy until you
    # know why, so it does not matter what else is in the list.
    Outcome.INFRA_FAILED: 0,
    # The model declared an impasse and explained itself: an acceptance criterion it believes cannot
    # be satisfied. If it is right, you are rewriting a spec, not a function.
    Outcome.IMPASSE: 1,
    # Green alone, red together. The defect is in how the work was cut across sub-issues, so the
    # diff to read is the *pair* of them.
    Outcome.INTEGRATION_FAILED: 2,
}

# An undeclared impasse: no spec to reconsider, just a diff to read, so it is opened last.
_UNDECLARED_IMPASSE = 3


def notify(
    graph: IssueGraph,
    states: Mapping[SubIssueId, SubIssueState],
    landed: Sequence[SubIssueId],
    failures: Mapping[SubIssueId, FailureReport],
    consumption: Mapping[SubIssueId, Sequence[SessionConsumption]],
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
        consumption=_summarize_consumption(graph, consumption),
        escalations=tuple(sorted(escalations, key=_open_this_one_first)),
    )


def _summarize_consumption(
    graph: IssueGraph, consumption: Mapping[SubIssueId, Sequence[SessionConsumption]]
) -> tuple[SubIssueConsumption, ...]:
    return tuple(
        SubIssueConsumption(
            sub_issue=id,
            consumption=total(record.consumption for record in consumption[id]),
            auto_compactions=sum(record.auto_compactions for record in consumption[id]),
        )
        for id in sorted(graph.sub_issues)
        if id in consumption and consumption[id]
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
