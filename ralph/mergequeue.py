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
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

from ralph.domain import SubIssue, SubIssueState
from ralph.events import event
from ralph.ports import Git, RunLog, TestRunner, Worktree


class LandResult(StrEnum):
    LANDED = "landed"
    REBASE_CONFLICT = "rebase-conflict"  # ─┐
    SUITE_RED = "suite-red"  #              ├─ all three become Outcome.INTEGRATION_FAILED
    FF_REFUSED = "ff-refused"  # ───────────┘
    HEAD_MOVED = "head-moved"


class MergeQueue:
    def __init__(self, git: Git, runner: TestRunner, integration: str, log: RunLog) -> None:
        self._git = git
        self._runner = runner
        self._integration = integration
        self._log = log
        self._merge_lock = asyncio.Lock()  # the merge lock. one process, so no flock, no PID files.

    async def land(self, sub: SubIssue, wt: Worktree) -> LandResult:
        async with self._merge_lock:
            if self._git.head_branch() != self._integration:
                # Somebody moved the base repo out from under us. Fast-forwarding now would move
                # a branch nobody asked us to move.
                return LandResult.HEAD_MOVED
            if not self._git.rebase(wt, self._integration):
                return LandResult.REBASE_CONFLICT
            if not (await self._runner.run(wt.path)).green:
                # Green in isolation, red on the prospective merge: the semantic conflict. The
                # Implementer could not have seen this about itself.
                return LandResult.SUITE_RED
            if not self._git.merge_ff_only(wt.branch):
                # Unreachable: we just rebased onto integration, so integration is an ancestor of
                # this branch by construction. If git refuses anyway, something we believe about
                # the repository is false — say so rather than reaching for a merge commit.
                return LandResult.FF_REFUSED

            # Merge first, then write. Fail toward redundant work, never toward missing code: a
            # crash between the two leaves a landed sub-issue marked unfinished, which costs a
            # re-run. The other order loses the commits.
            await self._log.write(event(sub.id, "terminal", SubIssueState.LANDED))
            return LandResult.LANDED
