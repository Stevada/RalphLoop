"""The run: read the graph, refuse a red base, dispatch on eligibility, quarantine, drain, notify.

**Sub-issues run in parallel but land one at a time.** The parallelism is here; the landing is the
merge queue's, and it is a separate lock for a reason — one sub-issue's integration failure must
never stall the queue for its siblings.

**There is no wave barrier.** Waves are an artifact of dependencies, not of merging. A sub-issue is
dispatched the moment its blockers land, not when its slowest sibling finishes, so the dispatch loop
below wakes on *every* completion and re-derives eligibility rather than draining a generation at a
time.

**Quarantine-and-drain.** A failure does not stop the run. The sub-issue is marked `needs-human`,
its worktree preserved as evidence, and *nothing is propagated through the graph*: its dependents
are not marked skipped, blocked, or failed — they are simply never eligible, because their blocker
never reaches LANDED. Eligibility is derived, never stored, and that is the whole mechanism. The
damage those failures cost is looked up once, at the end, for the notification.

**Nothing is ever retried.** There is no loop around a session here and no backoff anywhere. A
sub-issue gets one Implementer session; what follows is the Editor (#07), which is a different
actor reading a failure report, not the same actor having another go.

Still to come: the Editor and the cycle cap (#07), and the context ceiling (#08).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ralph.domain import (
    Actor,
    Destination,
    EventKind,
    FailureReport,
    Notification,
    Outcome,
    SubIssue,
    SubIssueId,
    SubIssueState,
    classify_implementer,
    eligible,
    failure_report,
    notify,
    route,
)
from ralph.events import event
from ralph.mergequeue import LandResult, MergeQueue
from ralph.ports import Budget, Git, Implementer, IssueStore, RunLog, TestRunner, Worktree

ACTIVE = Path(".worktrees") / "active"
QUARANTINE = Path(".worktrees") / "failed"

DEFAULT_CONCURRENCY = 4

log = logging.getLogger("ralph")


class BaseIsRed(RuntimeError):
    """The suite is red before a single session has started. Fatal.

    Not a warning, and not something to run anyway. A red base means a stale lockfile, a broken
    environment, a half-merged previous run, or trunk itself being broken — and every session
    dispatched into it would fail for a reason that has nothing to do with its sub-issue. The
    harness would classify N honest failures and pay an Editor to diagnose each one.
    """


@dataclass(frozen=True, slots=True)
class RunReport:
    """What the run produced. Thin on purpose: the `Notification` is the single source of truth,
    and everything below is a view of it rather than a second copy kept in step by hand."""

    notification: Notification

    @property
    def landed(self) -> tuple[SubIssueId, ...]:
        return self.notification.landed

    @property
    def failed(self) -> Mapping[SubIssueId, Outcome]:
        return {e.sub_issue: e.outcome for e in self.notification.escalations}

    @property
    def clean(self) -> bool:
        return not self.notification.escalations


@dataclass(frozen=True, slots=True)
class _Closed:
    """A finished pipeline. `report` is None exactly when the sub-issue landed."""

    sub_issue: SubIssueId
    report: FailureReport | None


class Scheduler:
    def __init__(
        self,
        *,
        repo: Path,
        git: Git,
        store: IssueStore,
        run_log: RunLog,
        runner: TestRunner,
        implementer: Implementer,
        merge_queue: MergeQueue,
        integration: str,
        budget: Budget,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._repo = repo
        self._git = git
        self._store = store
        self._run_log = run_log
        self._runner = runner
        self._implementer = implementer
        self._merge_queue = merge_queue
        self._integration = integration
        self._budget = budget
        self._concurrency = concurrency

    async def run(self) -> RunReport:
        await self._refuse_a_red_base()

        graph, states = self._store.read_graph()
        landed: list[SubIssueId] = []
        failures: dict[SubIssueId, FailureReport] = {}

        capacity = asyncio.Semaphore(self._concurrency)
        running: set[asyncio.Task[_Closed]] = set()

        try:
            while True:
                for id in sorted(eligible(graph, states)):
                    # Claimed synchronously, before this coroutine can yield. The state map is the
                    # only thing standing between a sub-issue and being dispatched twice, so it is
                    # updated in the same breath as the dispatch — not inside the task, which does
                    # not start running until the next suspension point.
                    states[id] = SubIssueState.IN_PROGRESS
                    running.add(asyncio.create_task(self._pipeline(graph.sub_issues[id], capacity)))

                if not running:
                    return RunReport(notify(graph, states, landed, failures))

                # FIRST_COMPLETED, not gather: the loop re-derives eligibility on every completion,
                # so a sub-issue starts the moment its blockers land rather than at the end of a
                # generation. This is what "no wave barrier" means in code.
                done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    closed = task.result()
                    if closed.report is None:
                        states[closed.sub_issue] = SubIssueState.LANDED
                        landed.append(closed.sub_issue)
                    else:
                        # Quarantine. Its dependents are not marked — they simply never become
                        # eligible, because this never reaches LANDED.
                        states[closed.sub_issue] = SubIssueState.NEEDS_HUMAN
                        failures[closed.sub_issue] = closed.report
        finally:
            # Only reachable when a pipeline raised — an infrastructure failure the harness has no
            # contingency for, on its way up. Leaving sessions running behind it would let them
            # write to a run log nobody is reading.
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    async def _refuse_a_red_base(self) -> None:
        base = await self._runner.run(self._repo)
        if not base.green:
            raise BaseIsRed(
                f"the suite is red on {self._integration} before any session has started. "
                f"No agent will be dispatched into a broken base.\n\n{base.output}"
            )

    async def _pipeline(self, sub: SubIssue, capacity: asyncio.Semaphore) -> _Closed:
        """One sub-issue, one session. What makes it a *cycle* — the Editor — arrives in #07.

        The semaphore is held for the whole pipeline, landing included: a sub-issue is not finished
        until it is on the integration branch, and counting it as free while it waits for the merge
        lock would let the cap be exceeded in the only place that matters.
        """
        async with capacity:
            await self._record(sub.id, "session-opened", SubIssueState.IN_PROGRESS)

            wt = self._git.add_worktree(
                f"ralph/{sub.id}", self._repo / ACTIVE / str(sub.id), self._integration
            )
            brief, findings = self._store.content(sub.id)

            telemetry = await self._implementer.run(brief, findings, wt, self._budget)
            # The suite result, not the exit code, is the outcome. The harness runs the tests.
            suite = await self._runner.run(wt.path)
            outcome = classify_implementer(telemetry, suite)
            detail: str | None = None

            if route(Actor.IMPLEMENTER, outcome) is Destination.MERGE_QUEUE:
                land = await self._merge_queue.land(wt)
                if land.result is LandResult.LANDED:
                    await self._record(sub.id, "session-closed", Outcome.SUCCESS)
                    # After the fast-forward, never before. Fail toward redundant work, never
                    # toward missing code.
                    await self._record(sub.id, "terminal", SubIssueState.LANDED)
                    return _Closed(sub.id, None)

                # Green in isolation, and it will not integrate. The Implementer could not have
                # observed this about itself — which is why it goes to the Editor (in #07) rather
                # than back to the actor that produced it.
                outcome, detail = Outcome.INTEGRATION_FAILED, land.result.value
                if land.suite is not None:
                    # The suite on the *prospective merge*, not the one that was green in the
                    # worktree as it stood. Handing the Editor an `integration-failed` alongside a
                    # green SuiteResult would be handing it a contradiction we manufactured.
                    suite = land.suite

            await self._record(sub.id, "session-closed", outcome)
            self._quarantine(sub.id, wt)
            await self._record(sub.id, "terminal", SubIssueState.NEEDS_HUMAN)
            return _Closed(sub.id, failure_report(outcome, telemetry, suite, detail))

    def _quarantine(self, id: SubIssueId, wt: Worktree) -> None:
        """The worktree is the evidence, and evidence is only preserved if it can be found.

        Raises if the destination is occupied — which means a previous run's wreckage for this same
        sub-issue is still sitting there un-triaged, and the sub-issue was authorised `ready` again
        anyway. That is worth stopping for; quietly clobbering it is not.
        """
        self._git.move_worktree(wt, self._repo / QUARANTINE / str(id))

    async def _record(
        self, id: SubIssueId, kind: EventKind, payload: Outcome | SubIssueState
    ) -> None:
        """Two sinks, different durability. The run log is **authoritative** — failing to write it
        fails the run. The issue store is **best-effort**: it mirrors the transition back into the
        tracker for a human's benefit, and a run must not die because the tracker was unreachable
        (a read-only file today; Linear being down tomorrow).
        """
        e = event(id, kind, payload)
        await self._run_log.write(e)
        try:
            await self._store.write_event(e)
        except Exception:  # noqa: BLE001 — best-effort by contract; the run log is the record
            log.warning(
                "could not mirror %s into the issue store; the run log stands", e, exc_info=True
            )
