"""Stamping an `Event` with the time it happened.

A one-function module, and it exists for a layering reason rather than a size one. `Event` is a
domain type, but reading the clock is reaching into the world — so the factory cannot live in
`domain/`, which is pure. It cannot live in `adapters/runlog.py` either: the merge queue and the
scheduler both need it, and orchestration may not import a concrete adapter.

So it lives here, between the two, importing nothing but the domain.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ralph.domain import Actor, Event, EventKind, Outcome, SubIssueId, SubIssueState, Verdict


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
