"""The run log's record type.

What is recorded: state transitions and outcomes. Deliberately *not* token spend, diffstats, or
failing-test output -- those belong in the failure report, which is a different artifact with a
different reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ralph.harness import Actor, Outcome, Verdict
from ralph.issues import SubIssueId, SubIssueState

EventDetails = Outcome | Verdict | SubIssueState
EventDetailsType = type[Outcome] | type[Verdict] | type[SubIssueState]


class EventKind(StrEnum):
    SESSION_STARTED = "session-started"
    SESSION_FINISHED = "session-finished"
    SUB_ISSUE_CLOSED = "sub-issue-closed"
    VERDICT_RECORDED = "verdict-recorded"


DETAILS_OF: dict[EventKind, EventDetailsType] = {
    EventKind.SESSION_STARTED: SubIssueState,
    EventKind.SESSION_FINISHED: Outcome,
    EventKind.SUB_ISSUE_CLOSED: SubIssueState,
    EventKind.VERDICT_RECORDED: Verdict,
}

_OLD_KIND_NAMES: dict[str, EventKind] = {
    "session-opened": EventKind.SESSION_STARTED,
    "session-closed": EventKind.SESSION_FINISHED,
    "terminal": EventKind.SUB_ISSUE_CLOSED,
    "verdict": EventKind.VERDICT_RECORDED,
}


def event_kind(value: str | EventKind) -> EventKind:
    if isinstance(value, EventKind):
        return value
    try:
        return EventKind(value)
    except ValueError:
        if value in _OLD_KIND_NAMES:
            return _OLD_KIND_NAMES[value]
        raise


@dataclass(frozen=True, slots=True)
class Event:
    ts: datetime
    sub_issue: SubIssueId
    actor: Actor
    """Whose session this is about.

    Load-bearing once the Editor exists: a cycle closes *two* sessions against the same sub-issue,
    and `session-finished: infra-failed` means something entirely different depending on whether the
    Implementer crashed or the Editor did. Without this field the authoritative record of the run
    cannot tell them apart, and a reader has to infer the actor from position -- which is exactly
    the kind of thing that is right until the day it matters.
    """

    kind: EventKind
    details: EventDetails

    def __post_init__(self) -> None:
        expected = DETAILS_OF[self.kind]
        if not isinstance(self.details, expected):
            raise ValueError(f"{self.kind.value} details must be {expected.__name__}")
