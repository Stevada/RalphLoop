"""The one notification, at the end of the run.

*A system that pages you the instant the first thing goes wrong trains you to ignore it.* So the
run drains, and this is the single artifact a human reads afterwards. The bar it has to clear:
**if you cannot tell from it alone whether to spend your first ten minutes reading a diff or
rewriting a PRD, the notification has failed.**

That bar is what shapes the type. An escalation is not a string — it carries the `FailureReport`
(the model's story checked against the harness's facts) and the sub-issues that never got a turn
because of it. The outcome says what *kind* of ten minutes you are about to spend; `stranded` says
what it cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from ralph.domain.model.failure import FailureReport
from ralph.domain.model.graph import SubIssueId
from ralph.domain.model.session import Outcome


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
