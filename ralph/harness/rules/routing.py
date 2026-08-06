"""The failure taxonomy, executable. One test per row.

Two outcomes need to know who is asking. SUCCESS, because an Implementer's goes to the merge queue,
an Editor's is a verdict to act on, and an Integrator's continues the landing it is already inside.
INTEGRATION_FAILED, because the same words mean different things: raised against an Implementer it
says two trees disagree and an Editor should look; raised against the Integrator it says the actor
sent to reconcile them could not, and nobody left is cheaper than a human.
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never

from ralph.harness.model.session import Actor, Outcome


class Destination(StrEnum):
    MERGE_QUEUE = "merge-queue"
    ACT_ON_VERDICT = "act-on-verdict"
    SUITE_GATE = "suite-gate"
    EDITOR = "editor"
    HUMAN = "human"
    # There is no retry destination. An Integrator is a different actor with a different job, not
    # the same session run again.


def route(actor: Actor, outcome: Outcome) -> Destination:
    """Where a classified session goes next.

    INFRA_FAILED goes straight to the human with the worktree preserved, and spends no cycle — a
    cycle is an Implementer session plus the Editor session that follows it, and no Editor is
    involved.
    """
    match outcome:
        case Outcome.SUCCESS:
            match actor:
                case Actor.IMPLEMENTER:
                    return Destination.MERGE_QUEUE
                case Actor.EDITOR:
                    return Destination.ACT_ON_VERDICT
                case Actor.INTEGRATOR:
                    # Back to the suite gate it is already inside — the queue never released the
                    # merge lock, so this is a continuation, not a second trip through the queue.
                    return Destination.SUITE_GATE
                case _:
                    assert_never(actor)
        case Outcome.INTEGRATION_FAILED if actor is Actor.INTEGRATOR:
            # The one `integration-failed` an Editor cannot help with. It is not evidence that a
            # spec is wrong: two correct trees disagreed textually, and the actor sent to reconcile
            # them could not.
            return Destination.HUMAN
        case Outcome.IMPASSE | Outcome.INTEGRATION_FAILED:
            return Destination.EDITOR
        case Outcome.INFRA_FAILED:
            return Destination.HUMAN
        case _:
            assert_never(outcome)
