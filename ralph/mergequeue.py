"""The merge queue: sub-issues run in parallel, but they land one at a time.

**This is the piece that makes parallelism honest**, and it is worth being precise about why.
The suite is re-run *in the worktree*, *after* the rebase — on the **prospective merge result**.
So the fast-forward that follows is only ever a fast-forward of an already-verified tree, and the
integration branch is **correct by construction**.

Run the suite before the rebase and you have verified a tree that is not the one you are landing.
Run it in the base checkout and you have verified a tree that does not contain the work. Both
mistakes produce a green integration branch that is broken, which is the exact failure the whole
harness exists to make impossible.

The merge lock is held for rebase → suite → fast-forward, and for nothing else. It is never held
while an Editor reasons: one sub-issue's integration failure must not stall the queue for its
siblings.

It writes nothing anywhere. The queue's whole job is to decide whether this tree may become the
integration branch, and to say so; recording *that* a sub-issue landed is a state transition, and
state transitions belong to the scheduler. Two writers for one fact is one writer too many.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from ralph.domain import SuiteResult
from ralph.ports import Git, TestRunner, Worktree


class LandResult(StrEnum):
    LANDED = "landed"
    REBASE_CONFLICT = "rebase-conflict"  # ─┐
    SUITE_RED = "suite-red"  #              ├─ all three become Outcome.INTEGRATION_FAILED
    FF_REFUSED = "ff-refused"  # ───────────┘
    HEAD_MOVED = "head-moved"


@dataclass(frozen=True, slots=True)
class Land:
    """The outcome, and the evidence for it.

    `suite` is the run on the **prospective merge result** — present whenever the queue got far
    enough to do one. It is carried out of here because it is the only honest suite result for an
    `integration-failed` sub-issue: the worktree's own run was green (that is why it reached the
    queue at all), and handing the Editor that green result alongside an integration failure would
    be handing it a contradiction the harness manufactured.
    """

    result: LandResult
    suite: SuiteResult | None = None


class MergeQueue:
    def __init__(self, git: Git, runner: TestRunner, integration: str) -> None:
        self._git = git
        self._runner = runner
        self._integration = integration
        self._merge_lock = asyncio.Lock()  # the merge lock. one process, so no flock, no PID files.

    async def land(self, wt: Worktree) -> Land:
        """A worktree is all it needs. It does not care which sub-issue this is, and once it stopped
        writing the landed event it had no further use for the name."""
        async with self._merge_lock:
            if self._git.head_branch() != self._integration:
                # Somebody moved the base repo out from under us. Fast-forwarding now would move
                # a branch nobody asked us to move.
                return Land(LandResult.HEAD_MOVED)
            if not self._git.rebase(wt, self._integration):
                # No retry, and no backoff. A rebase conflict is a signal about how the work was
                # cut, not a transient hiccup that a second attempt would get past.
                return Land(LandResult.REBASE_CONFLICT)

            suite = await self._runner.run(wt.path)
            if not suite.green:
                # Green in isolation, red on the prospective merge: the semantic conflict. The
                # Implementer could not have seen this about itself.
                return Land(LandResult.SUITE_RED, suite)
            if not self._git.merge_ff_only(wt.branch):
                # Unreachable: we just rebased onto integration, so integration is an ancestor of
                # this branch by construction. If git refuses anyway, something we believe about
                # the repository is false — say so rather than reaching for a merge commit.
                return Land(LandResult.FF_REFUSED, suite)

            return Land(LandResult.LANDED, suite)
