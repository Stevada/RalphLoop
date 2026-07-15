"""The notification values produced at the end of a run."""

from __future__ import annotations

from dataclasses import dataclass

from ralph.harness import FailureReport, Outcome
from ralph.issues.graph import SubIssueId


@dataclass(frozen=True, slots=True)
class Escalation:
    """One sub-issue that reached `needs-human`, and what it is holding up."""

    sub_issue: SubIssueId
    outcome: Outcome
    report: FailureReport
    stranded: tuple[SubIssueId, ...]  # never got a turn, because this one never landed


@dataclass(frozen=True, slots=True)
class Notification:
    landed: tuple[SubIssueId, ...]
    escalations: tuple[Escalation, ...]  # ranked: the one to open first comes first
