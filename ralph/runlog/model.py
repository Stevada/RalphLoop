"""The run log's record type.

What is recorded: state transitions and outcomes. Deliberately *not* token spend, diffstats, or
failing-test output -- those belong in the failure report, which is a different artifact with a
different reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ralph.domain import Actor, Outcome, SubIssueId, SubIssueState, Verdict

EventKind = Literal["session-opened", "session-closed", "terminal", "verdict"]


@dataclass(frozen=True, slots=True)
class Event:
    ts: datetime
    sub_issue: SubIssueId
    actor: Actor
    """Whose session this is about.

    Load-bearing once the Editor exists: a cycle closes *two* sessions against the same sub-issue,
    and `session-closed: infra-failed` means something entirely different depending on whether the
    Implementer crashed or the Editor did. Without this field the authoritative record of the run
    cannot tell them apart, and a reader has to infer the actor from position -- which is exactly
    the kind of thing that is right until the day it matters.
    """

    kind: EventKind
    payload: Outcome | Verdict | SubIssueState
