"""The run: read the graph, refuse a red base, dispatch on eligibility, quarantine, drain, notify.

Sub-issues run in parallel but land one at a time (the merge queue's lock).
There is no wave barrier: the dispatch loop re-derives eligibility on every completion, so a
sub-issue starts the moment its blockers land. A failure is quarantined and drained around, never
retried — the loop below is a *cycle* loop, not a retry loop, and that distinction, quarantine-and-
drain, and the scheduler-only cycle cap are the design (`docs/prd.md` §4.5–§4.7 for the reasoning,
`docs/architecture.md` §4 for the shape).

The comments in this module explain only what the code cannot: the ordering the async scheduler
depends on. The design arguments they once restated now live in prd.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ralph.harness import (
    Actor,
    CycleLedger,
    Destination,
    FailureReport,
    Outcome,
    Verdict,
    classify_editor,
    classify_implementer,
    eligible,
    failure_report,
    route,
)
from ralph.issues import SubIssue, SubIssueId, SubIssueState
from ralph.issues.consumption import SessionConsumption
from ralph.issues.store import IssueStore
from ralph.mergequeue import LandResult, MergeQueue
from ralph.notification import Notification, notify
from ralph.ports import (
    Budget,
    Editor,
    Git,
    Implementer,
    RunLog,
    SessionContext,
    TestRunner,
    Worktree,
)
from ralph.runlog import EventKind, event

log = logging.getLogger("ralph")

ACTIVE = Path(".worktrees") / "active"
QUARANTINE = Path(".worktrees") / "failed"


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
    """The run.

    Every run has an Editor. The composition root resolves the concrete adapter before the
    scheduler starts, so a failed Implementer session that routes to adjudication always reaches
    the Editor loop.
    """

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
        editor: Editor,
    ) -> None:
        self._repo = repo
        self._git = git
        self._store = store
        self._run_log = run_log
        self._runner = runner
        self._implementer = implementer
        self._editor = editor
        self._merge_queue = merge_queue
        self._integration = integration
        self._budget = budget
        self._ledger = CycleLedger()

    async def run(self) -> RunReport:
        await self._refuse_a_red_base()

        graph, states = self._store.read_graph()
        landed: list[SubIssueId] = []
        failures: dict[SubIssueId, FailureReport] = {}

        running: set[asyncio.Task[_Closed]] = set()

        try:
            while True:
                for id in sorted(eligible(graph, states)):
                    # Claimed synchronously, before this coroutine can yield. The state map is the
                    # only thing standing between a sub-issue and being dispatched twice, so it is
                    # updated in the same breath as the dispatch — not inside the task, which does
                    # not start running until the next suspension point.
                    states[id] = SubIssueState.IN_PROGRESS
                    running.add(asyncio.create_task(self._pipeline(graph.sub_issues[id])))

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

    async def _pipeline(self, sub: SubIssue) -> _Closed:
        """One sub-issue, to a terminal state — however many cycles that takes.

        A sub-issue is not finished until it is on the integration branch. The merge queue, not the
        scheduler, serializes the landing step.
        """
        while True:
            closed = await self._cycle(sub)
            if closed is not None:
                return closed
            # `revise`: the work is discarded and the sub-issue restarts clean against the
            # rewritten brief. This cannot spin — `_cycle` returns None only after spending a
            # cycle, and the third spend makes `must_be_terminal` true, which no longer lets a
            # `revise` through.

    async def _cycle(self, sub: SubIssue) -> _Closed | None:
        """One Implementer session, plus the Editor session that follows it if it failed.

        Returns the terminal state, or `None` to mean *the Editor said `revise`; go round again*.
        """
        attempt = self._ledger.spent(sub.id) + 1
        await self._record(
            sub.id, Actor.IMPLEMENTER, EventKind.SESSION_STARTED, SubIssueState.IN_PROGRESS
        )

        wt = self._git.add_worktree(
            f"ralph/{sub.id}", self._repo / ACTIVE / str(sub.id), self._integration
        )
        # The newest revision, or the Planner's original if the Editor has never touched this. On
        # cycle two this is the **rewritten** brief — which is what makes this a cycle, not a retry.
        brief, findings = self._store.content(sub.id)

        context = SessionContext(brief=brief, findings=findings, worktree=wt, budget=self._budget)
        telemetry = await self._implementer.run(context)
        await self._store.record_consumption(
            sub.id,
            SessionConsumption(
                actor=Actor.IMPLEMENTER, consumed_tokens=telemetry.consumed_tokens
            ),
        )
        # The suite result, not the exit code, is the outcome. The harness runs the tests.
        suite = await self._runner.run(wt.path)
        outcome = classify_implementer(telemetry, suite)
        detail: str | None = None

        if route(Actor.IMPLEMENTER, outcome) is Destination.MERGE_QUEUE:
            land = await self._merge_queue.land(wt)
            if land.result is LandResult.LANDED:
                await self._record(
                    sub.id, Actor.IMPLEMENTER, EventKind.SESSION_FINISHED, Outcome.SUCCESS
                )
                # After the fast-forward, never before. Fail toward redundant work, never toward
                # missing code.
                await self._record(
                    sub.id, Actor.IMPLEMENTER, EventKind.SUB_ISSUE_CLOSED, SubIssueState.LANDED
                )
                return _Closed(sub.id, None)

            # Green in isolation, and it will not integrate. The Implementer could not have observed
            # this about itself — which is why it goes to the Editor rather than back to the actor
            # that produced it, and why it spends a cycle exactly like an impasse does.
            outcome, detail = Outcome.INTEGRATION_FAILED, land.result.value
            if land.suite is not None:
                # The suite on the *prospective merge*, not the one that was green in the worktree
                # as it stood. Handing the Editor an `integration-failed` alongside a green
                # SuiteResult would be handing it a contradiction we manufactured.
                suite = land.suite

        await self._record(sub.id, Actor.IMPLEMENTER, EventKind.SESSION_FINISHED, outcome)
        report = failure_report(outcome, telemetry, suite, detail, attempt)

        if route(Actor.IMPLEMENTER, outcome) is not Destination.EDITOR:
            # `infra-failed`: the outcome no Editor can help with. It goes straight to the human
            # and spends no cycle: a cycle is an Implementer session plus an Editor session, and no
            # Editor is involved.
            return await self._quarantine(sub.id, Actor.IMPLEMENTER, wt, report)

        return await self._adjudicate(sub, context, report)

    async def _adjudicate(
        self,
        sub: SubIssue,
        context: SessionContext,
        report: FailureReport,
    ) -> _Closed | None:
        """The Editor half of the cycle: read the failure, return a verdict, and act on it.

        **The Editor never writes code.** It reads the brief, the findings, the failure report and
        the failed worktree, and it returns a judgment. Every *consequence* of that judgment —
        storing the revision, writing the run log, spending the cycle, refusing a fourth — happens
        here, in the scheduler, and nowhere else. The port takes no `RunLog` and no `IssueStore` on
        purpose.
        """
        # A cycle is spent the moment an Implementer failure routes to the Editor: that is when the
        # Editor half begins. Spent *before* the session, so that a killed Editor still costs one —
        # otherwise an Editor that reliably times out would buy a sub-issue infinite Implementers,
        # and the cap would hold only along the paths that were working anyway.
        self._ledger.spend(sub.id)
        must_be_terminal = self._ledger.must_be_terminal(sub.id)

        await self._record(
            sub.id, Actor.EDITOR, EventKind.SESSION_STARTED, SubIssueState.IN_PROGRESS
        )
        telemetry, verdict = await self._editor.adjudicate(context, report, must_be_terminal)
        await self._store.record_consumption(
            sub.id,
            SessionConsumption(actor=Actor.EDITOR, consumed_tokens=telemetry.consumed_tokens),
        )
        outcome = classify_editor(telemetry, verdict)
        await self._record(sub.id, Actor.EDITOR, EventKind.SESSION_FINISHED, outcome)

        if route(Actor.EDITOR, outcome) is Destination.HUMAN or verdict is None:
            # The Editor itself was killed, or came back with nothing. Escalate on the **Editor's**
            # outcome and telemetry: the Implementer's failure is no longer the interesting fact —
            # that the harness cannot adjudicate it is. A human told `impasse` here would go and
            # rewrite a brief, when what actually needs fixing is the Editor.
            #
            # (`verdict is None` is already covered by the route above: `classify_editor` calls a
            # verdictless Editor `infra-failed`, which routes to the human. It is restated only
            # because the type-checker cannot read the taxonomy.)
            failed = failure_report(outcome, telemetry, report.suite, None, report.cycles)
            return await self._quarantine(sub.id, Actor.EDITOR, context.worktree, failed)

        await self._record(sub.id, Actor.EDITOR, EventKind.VERDICT_RECORDED, verdict.verdict)

        if verdict.verdict.is_terminal:
            # `planning-defect` — the brief cannot be satisfied as written, and rewriting it is a
            # Planner's call, not an Editor's. `inconclusive` — the Editor could not tell. Both are
            # terminal: another Implementer session would be a coin flip we have already paid for.
            return await self._quarantine(sub.id, Actor.EDITOR, context.worktree, report)

        if must_be_terminal:
            # **The scheduler rejects it, and only the scheduler.** The Editor was told this was the
            # final cycle and asked for another anyway. Its verdict is refused rather than obeyed:
            # the cap is the harness's rule, and a rule enforced by asking a model nicely is not a
            # rule. The sub-issue escalates on the Implementer's failure, which is what a human
            # needs to see.
            log.warning(
                "%s: the Editor returned `revise` on its final cycle; refusing a fourth "
                "Implementer session. Escalating to a human.",
                sub.id,
            )
            return await self._quarantine(sub.id, Actor.EDITOR, context.worktree, report)

        revised_brief, revised_findings = verdict.revision
        # Knowledge survives **only** through the findings. Nothing else crosses: the diff is
        # discarded, the transcript is discarded, and what the Editor chose to write down is all the
        # next session gets. That choice is the Editor's judgment, unmandated — and keeping it out
        # of the brief is what lets a session be *helped* without the bar being *lowered*.
        await self._store.record_revision(
            sub.id,
            revised_brief,
            revised_findings if revised_findings is not None else context.findings,
        )
        self._git.discard_worktree(context.worktree)
        return None

    async def _quarantine(
        self, id: SubIssueId, actor: Actor, wt: Worktree, report: FailureReport
    ) -> _Closed:
        """The worktree is the evidence, and evidence is only preserved if it can be found.

        Raises if the destination is occupied — which means a previous run's wreckage for this same
        sub-issue is still sitting there un-triaged, and the sub-issue was authorised `ready` again
        anyway. That is worth stopping for; quietly clobbering it is not.
        """
        self._git.move_worktree(wt, self._repo / QUARANTINE / str(id))
        await self._record(id, actor, EventKind.SUB_ISSUE_CLOSED, SubIssueState.NEEDS_HUMAN)
        return _Closed(id, report)

    async def _record(
        self,
        id: SubIssueId,
        actor: Actor,
        kind: EventKind,
        details: Outcome | Verdict | SubIssueState,
    ) -> None:
        """Two sinks, different durability. The run log is **authoritative** — failing to write it
        fails the run. The issue store is **best-effort**: it mirrors the transition back into the
        tracker for a human's benefit, and a run must not die because the tracker was unreachable
        (a read-only file today; Linear being down tomorrow).
        """
        e = event(id, actor, kind, details)
        await self._run_log.write(e)
        try:
            await self._store.write_event(e)
        except Exception:  # noqa: BLE001 — best-effort by contract; the run log is the record
            log.warning(
                "could not mirror %s into the issue store; the run log stands", e, exc_info=True
            )
