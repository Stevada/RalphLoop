"""The merge gate: sub-issues run in parallel, but they land one at a time.

**This is the piece that makes parallelism honest**, and it is worth being precise about why.
The suite is re-run *in the worktree*, *after* the integration branch is merged in — on the
**prospective merge result**. So the fast-forward that follows is only ever a fast-forward of an
already-green tree, and the integration branch is **correct by construction**.

Run the suite before the merge and you have run it against a tree that is not the one you are
landing. Run it in the base checkout and you have run it against a tree that does not contain the
work. Both mistakes produce a green integration branch that is broken, which is the exact failure
the whole harness exists to make impossible.

The merge lock is held for merge → reconciliation → suite → fast-forward, and for nothing else. It
is never held while an *Editor* reasons: a red prospective merge releases it and goes away to be
adjudicated, so one sub-issue's semantic failure does not stall the gate for its siblings.

A **conflict** is the exception, and deliberately so. Reconciliation happens inside the lock,
because its result is only valid against the integration head that produced the conflict; release
the lock to think and a sibling lands underneath, leaving a resolution against a tip that no longer
exists. That costs throughput on a run where conflicts are frequent and buys the fast-forward below
its correctness. The sub-issue never comes back here: it lands, or it goes to a human.

It writes nothing anywhere — not even about the session it dispatches. The gate's whole job is to
decide whether this tree may become the integration branch, and to say so; recording *that* a
sub-issue landed, or what a session cost, is a state transition, and state transitions belong to the
scheduler. Two writers for one fact is one writer too many.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from ralph.harness import Outcome, SessionTelemetry, SuiteResult, classify_integrator
from ralph.ports import Budget, Candidate, Git, Integrator, SessionContext, TestRunner


class LandResult(StrEnum):
    LANDED = "landed"
    SUITE_RED = "suite-red"  # ─┐
    FF_REFUSED = "ff-refused"  # ├─ both become Outcome.INTEGRATION_FAILED, and go to the Editor
    HEAD_MOVED = "head-moved"  # ┘
    CONFLICT_UNRESOLVED = "conflict-unresolved"
    """The Integrator was dispatched and the merge is still open. Straight to a human: there is no
    Editor move here, because no spec was wrong."""


@dataclass(frozen=True, slots=True)
class Land:
    """The outcome, and the evidence for it.

    `suite` is the run on the **prospective merge result** — present whenever the gate got far
    enough to do one. It is carried out of here because it is the only honest suite result for an
    `integration-failed` sub-issue: the worktree's own run was green (that is why it reached the
    gate at all), and handing the Editor that green result alongside an integration failure would
    be handing it a contradiction the harness manufactured.

    `integrator` is the reconciliation session, when there was one — present on **every** exit,
    including the ones it succeeded at, because a session that cost tokens has to be billed whether
    or not it changed the outcome. `integrator_outcome` is the gate's classification of it, carried
    rather than recomputed: the scheduler cannot re-derive it without re-asking git a question whose
    answer has since moved on. The two travel together — either both are set or neither is.
    """

    result: LandResult
    suite: SuiteResult | None = None
    integrator: SessionTelemetry | None = None
    integrator_outcome: Outcome | None = None

class MergeGate:
    def __init__(
        self,
        git: Git,
        runner: TestRunner,
        integration_branch: str,
        integrator: Integrator,
        budget: Budget,
    ) -> None:
        self._git = git
        self._runner = runner
        self._integration_branch = integration_branch
        self._integrator = integrator
        # Held because the gate admits a candidate but dispatches a *session*, and only a session
        # is bounded. The candidate carries no budget; that is what makes it not a session context.
        self._budget = budget
        self._merge_lock = asyncio.Lock()  # the merge lock. one process, so no flock, no PID files.
        self._landings: set[asyncio.Task[Land]] = set()

    async def land(self, candidate: Candidate) -> Land:
        """Take the merge lock when it comes free, and land or don't.

        **The landing is not cancellable by its caller.** It runs as its own task behind a shield,
        so a pipeline cancelled mid-flight — which the scheduler does to every in-flight task when
        another one raises — stops waiting without stopping the merge. Cancelled in place, a
        landing would take `CancelledError` at an await *inside* the lock, leave a half-merged
        worktree, and release the lock; the next candidate would then merge onto a tree nobody
        verified. Losing the answer is fine. Losing it halfway through `git merge` is not.
        """
        landing = asyncio.ensure_future(self._land(candidate))
        self._landings.add(landing)
        landing.add_done_callback(self._landings.discard)
        return await asyncio.shield(landing)

    async def drain(self) -> None:
        """Wait out any landing whose caller has already gone away.

        The shield above means a cancelled pipeline leaves a live task behind, and the run must not
        exit from under it: the loop shutting down would cancel it exactly where cancelling it was
        the thing worth preventing.
        """
        if self._landings:
            await asyncio.gather(*self._landings, return_exceptions=True)

    async def _land(self, candidate: Candidate) -> Land:
        """The whole landing, start to finish, under one hold of the lock.

        A conflict is answered here rather than returned, because the answer depends on the
        integration head being where it was when the conflict was found. Release the lock to think
        about it and a sibling can land underneath, leaving a reconciliation against a branch that
        no longer exists as anyone's tip.
        """
        wt = candidate.worktree
        async with self._merge_lock:
            if self._git.head_branch() != self._integration_branch:
                # Somebody moved the base repo out from under us. Fast-forwarding now would move
                # a branch nobody asked us to move.
                return Land(LandResult.HEAD_MOVED)

            reconciliation: SessionTelemetry | None = None
            reconciled: Outcome | None = None
            if not self._git.merge(wt, self._integration_branch):
                # Not a retry, and not a hiccup to back off from: a different actor, answering a
                # question about landing order that the Implementer could not have seen. The
                # conflict is left exactly as git made it — that is this session's input.
                reconciliation = await self._integrator.reconcile(
                    SessionContext(candidate=candidate, budget=self._budget)
                )
                reconciled = classify_integrator(
                    reconciliation, self._git.merge_finished(wt)
                )
                if reconciled is not Outcome.SUCCESS:
                    return Land(
                        LandResult.CONFLICT_UNRESOLVED,
                        None,
                        reconciliation,
                        reconciled,
                    )

            suite = await self._runner.run(wt.path)
            if not suite.green:
                # Green in isolation, red on the prospective merge: the semantic conflict. The
                # Implementer could not have seen this about itself.
                return Land(LandResult.SUITE_RED, suite, reconciliation, reconciled)
            if not self._git.merge_ff_only(wt.branch):
                # Unreachable: we just merged integration into this branch, so integration is an
                # ancestor of it by construction. If git refuses anyway, something we believe about
                # the repository is false — say so rather than reaching for a merge commit.
                return Land(LandResult.FF_REFUSED, suite, reconciliation, reconciled)

            return Land(LandResult.LANDED, suite, reconciliation, reconciled)
