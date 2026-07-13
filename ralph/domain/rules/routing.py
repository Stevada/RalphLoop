"""The failure taxonomy, executable. One test per row.

`ceiling-exceeded` and `infra-failed` route identically for both actors; only SUCCESS needs to
know who is asking, because an Implementer's success goes to the merge queue and an Editor's
success is a verdict to act on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never

from ralph.domain.model.session import Actor, Outcome


class Destination(StrEnum):
    MERGE_QUEUE = "merge-queue"
    ACT_ON_VERDICT = "act-on-verdict"
    EDITOR = "editor"
    HUMAN = "human"
    # There is no retry destination. There is no retry anywhere in this system.


def route(actor: Actor, outcome: Outcome) -> Destination:
    """Where a classified session goes next.

    CEILING_EXCEEDED and INFRA_FAILED are the two outcomes the Editor never sees, from either
    actor, and neither is ever retried. Both go straight to the human with the worktree preserved,
    and both spend no cycle — a cycle is an Implementer session plus the Editor session that
    follows it, and no Editor is involved in either.
    """
    match outcome:
        case Outcome.SUCCESS:
            match actor:
                case Actor.IMPLEMENTER:
                    return Destination.MERGE_QUEUE
                case Actor.EDITOR:
                    return Destination.ACT_ON_VERDICT
                case _:
                    assert_never(actor)
        case Outcome.IMPASSE | Outcome.SILENT_RED | Outcome.INTEGRATION_FAILED:
            return Destination.EDITOR
        case Outcome.CEILING_EXCEEDED | Outcome.INFRA_FAILED:
            return Destination.HUMAN
        case _:
            assert_never(outcome)
