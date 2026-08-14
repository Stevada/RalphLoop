"""Run-log values, stamping, and JSONL storage.

This module is the interface. The run log is the harness's authoritative story of what happened,
while the issue store only mirrors the same events for human convenience.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ralph.harness import Actor
from ralph.issues import SubIssueId
from ralph.runlog.jsonl import JsonlRunLog, RunLogError
from ralph.runlog.model import Event, EventDetails, EventKind


def event(
    sub_issue: SubIssueId,
    actor: Actor,
    kind: EventKind,
    details: EventDetails,
    at: datetime | None = None,
) -> Event:
    """UTC, because a run log gets read across a timezone boundary at 3am.

    `at` is for an event that can only be written after the fact — where the moment of writing is
    not the moment described, and stamping `now` would put a false time in the authoritative
    record. Everything observed as it happens leaves it alone.
    """
    return Event(
        ts=at if at is not None else datetime.now(UTC),
        sub_issue=sub_issue,
        actor=actor,
        kind=kind,
        details=details,
    )


__all__ = [
    "Event",
    "EventDetails",
    "EventKind",
    "JsonlRunLog",
    "RunLogError",
    "event",
]
