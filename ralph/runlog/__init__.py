"""Run-log values, stamping, and JSONL storage.

This module is the interface. The run log is the harness's authoritative story of what happened,
while the issue store only mirrors the same events for human convenience.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ralph.domain import Actor, Outcome, SubIssueId, SubIssueState, Verdict
from ralph.runlog.jsonl import JsonlRunLog, RunLogError
from ralph.runlog.model import Event, EventKind


def event(
    sub_issue: SubIssueId,
    actor: Actor,
    kind: EventKind,
    payload: Outcome | Verdict | SubIssueState,
) -> Event:
    """UTC, because a run log gets read across a timezone boundary at 3am."""
    return Event(
        ts=datetime.now(UTC), sub_issue=sub_issue, actor=actor, kind=kind, payload=payload
    )


__all__ = [
    "Event",
    "EventKind",
    "JsonlRunLog",
    "RunLogError",
    "event",
]
