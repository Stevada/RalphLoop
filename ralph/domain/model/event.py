"""The run log's record type.

Lives in `domain/` rather than `runlog.py` because `IssueStore.write_event` takes one, and
`ports.py` may not import orchestration — the dependency arrow points inward. `runlog.py` imports
it from here.

What is recorded: state transitions and outcomes. Deliberately *not* token spend, diffstats, or
failing-test output — those belong in the failure report, which is a different artifact with a
different reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ralph.domain.model.graph import SubIssueId
from ralph.domain.model.state import SubIssueState
from ralph.domain.model.session import Outcome
from ralph.domain.model.verdict import Verdict

EventKind = Literal["session-opened", "session-closed", "terminal", "verdict"]


@dataclass(frozen=True, slots=True)
class Event:
    ts: datetime
    sub_issue: SubIssueId
    kind: EventKind
    payload: Outcome | Verdict | SubIssueState
