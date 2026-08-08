"""The run: read the graph, dispatch on eligibility, quarantine, drain, notify.

Sub-issues run in parallel but land one at a time (the merge gate's lock). `sequential` narrows
that to one sub-issue in flight at a time; it changes what is dispatched and nothing else, because
everything that makes a landing correct lives in the merge gate.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ralph.harness import (
    TokenConsumption,
    Actor,
    CycleLedger,
    Destination,
    FailureReport,
    Outcome,
    SessionTelemetry,
    SuiteResult,
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
from ralph.mergegate import Land, LandResult, MergeGate
from ralph.notification import Notification, notify
from ralph.ports import (
    Budget,
    Candidate,
    Editor,
    FinishedSession,
    Git,
    Implementer,
    RunLog,
    SessionContext,
    Transcripts,
)
from ralph.runlog import EventKind, event

log = logging.getLogger("ralph")

ACTIVE = Path(".worktrees") / "active"
QUARANTINE = Path(".worktrees") / "failed"


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
    def consumption(self) -> Mapping[SubIssueId, TokenConsumption]:
        return {c.sub_issue: c.consumption for c in self.notification.consumption}

    @property
    def total_consumption(self) -> TokenConsumption:
        return self.notification.total_consumption

    @property
    def auto_compactions(self) -> Mapping[SubIssueId, int]:
        return {c.sub_issue: c.auto_compactions for c in self.notification.consumption}

    @property
    def total_auto_compactions(self) -> int:
        return self.notification.total_auto_compactions

    @property
    def clean(self) -> bool:
        return not self.notification.escalations


@dataclass(frozen=True, slots=True)
class _Closed:
    """A finished pipeline. `report` is None exactly when the sub-issue landed."""

    sub_issue: SubIssueId
    report: FailureReport | None


@dataclass(frozen=True, slots=True)
class _FailedLanding:
    """A merge-gate failure, normalized into the Editor's input shape."""

    outcome: Outcome
    telemetry: SessionTelemetry
    suite: SuiteResult | None
    detail: str | None


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
        transcripts: Transcripts,
        implementer: Implementer,
        merge_gate: MergeGate,
        integration_branch: str,
        budget: Budget,
        editor: Editor,
        parent_issue_name: str | None = None,
        sequential: bool = False,
    ) -> None:
        self._repo = repo
        self._git = git
        self._store = store
        self._run_log = run_log
        self._transcripts = transcripts
        self._implementer = implementer
        self._editor = editor
        self._merge_gate = merge_gate
        self._integration_branch = integration_branch
        self._budget = budget
        self._ledger = CycleLedger()
        self._parent_issue_name = parent_issue_name
        self._sequential = sequential

    def _worktree_dir(self, base: Path, id: SubIssueId) -> Path:
        """Nested under the parent issue's name when known, so two phases sharing a repo never
        collide on a bare sub-issue number."""
        if self._parent_issue_name is None:
            return self._repo / base / str(id)
        return self._repo / base / self._parent_issue_name / str(id)

    def _branch(self, id: SubIssueId) -> str:
        if self._parent_issue_name is None:
            return f"ralph/{id}"
        return f"ralph/{self._parent_issue_name}_{id}"

    async def run(self) -> RunReport:
        graph, states = self._store.read_graph()
        consumption_offsets = {
            id: len(self._store.consumption(id)) for id in graph.sub_issues
        }
        landed: list[SubIssueId] = []
        failures: dict[SubIssueId, FailureReport] = {}

        running: set[asyncio.Task[_Closed]] = set()

        try:
            while True:
                for id in sorted(eligible(graph, states)):
                    if self._sequential and running:
                        # One in flight at a time. The rest stay `ready` and are re-derived on the
                        # next completion, so a sub-issue whose blockers landed meanwhile is still
                        # picked up in the same order it would have been.
                        break
                    # Claimed synchronously, before this coroutine can yield. The state map is the
                    # only thing standing between a sub-issue and being dispatched twice, so it is
                    # updated in the same breath as the dispatch — not inside the task, which does
                    # not start running until the next suspension point.
                    states[id] = SubIssueState.IN_PROGRESS
                    running.add(asyncio.create_task(self._pipeline(graph.sub_issues[id])))

                if not running:
                    consumption = {
                        id: self._store.consumption(id)[consumption_offsets[id] :]
                        for id in graph.sub_issues
                    }
                    return RunReport(notify(graph, states, landed, failures, consumption))

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
            # A cancelled pipeline stops waiting for its landing; it does not stop the landing.
            # Returning here would let the loop close on a task holding the merge lock, mid-merge.
            await self._merge_gate.drain()

    async def _pipeline(self, sub: SubIssue) -> _Closed:
        """One sub-issue, to a terminal state — however many cycles that takes.

        A sub-issue is not finished until it is on the integration branch. The merge gate, not the
        scheduler, serializes the landing step.
        """
        while True:
            closed = await self._cycle(sub)
            if closed is not None:
                return closed
            # `revise`: the work is discarded and the sub-issue restarts clean against the
            # rewritten spec. This cannot spin — `_cycle` returns None only after spending a
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
            self._branch(sub.id), self._worktree_dir(ACTIVE, sub.id), self._integration_branch
        )
        # The newest revision, or the Planner's original if the Editor has never touched this. On
        # cycle two this is the **rewritten** spec — which is what makes this a cycle, not a retry.
        spec, findings = self._store.content(sub.id)

        candidate = Candidate(id=sub.id, spec=spec, findings=findings, worktree=wt)
        context = SessionContext(candidate=candidate, budget=self._budget)
        telemetry = await self._implementer.run(context)
        # Classified before it is recorded, because the transcript's footer carries the conclusion
        # beside the observations it was drawn from — and a conclusion cannot be written down
        # before it is reached.
        outcome = classify_implementer(telemetry)
        await self._record_session(sub.id, attempt, Actor.IMPLEMENTER, outcome, telemetry)
        suite = None
        detail: str | None = None

        if route(Actor.IMPLEMENTER, outcome) is Destination.MERGE_GATE:
            land = await self._merge_gate.land(candidate)
            await self._record_reconciliation(sub.id, attempt, land)

            if land.result is LandResult.LANDED:
                return await self._landed(candidate)

            if land.result is LandResult.CONFLICT_UNRESOLVED:
                # The Integrator was dispatched and the merge is still open. This escalates on the
                # **Integrator's** outcome and telemetry, not the Implementer's: the Implementer
                # delivered, and what a human needs to see is the reconciliation that failed.
                assert land.integrator is not None and land.integrator_outcome is not None
                return await self._quarantine(
                    candidate,
                    Actor.INTEGRATOR,
                    failure_report(
                        land.integrator_outcome,
                        land.integrator,
                        None,
                        land.result.value,
                        attempt,
                    ),
                )

            # The suite gate is the harness's first run of the suite. A red prospective merge goes
            # to the Editor with that gate's evidence, and spends a cycle exactly like an impasse.
            outcome, detail = Outcome.INTEGRATION_FAILED, land.result.value
            if land.suite is not None:
                # The suite on the *prospective merge*. This is the only suite evidence the
                # scheduler is allowed to hand the Editor for an Implementer failure.
                suite = land.suite

        await self._record(sub.id, Actor.IMPLEMENTER, EventKind.SESSION_FINISHED, outcome)
        report = failure_report(outcome, telemetry, suite, detail, attempt)

        if route(Actor.IMPLEMENTER, outcome) is not Destination.EDITOR:
            # `infra-failed`: the outcome no Editor can help with. It goes straight to the human
            # and spends no cycle: a cycle is an Implementer session plus an Editor session, and no
            # Editor is involved.
            return await self._quarantine(candidate, Actor.IMPLEMENTER, report)

        # Editor's half
        return await self._adjudicate(context, report)

    async def _landed(self, candidate: Candidate) -> _Closed:
        id = candidate.id
        await self._record(id, Actor.IMPLEMENTER, EventKind.SESSION_FINISHED, Outcome.SUCCESS)
        # After the fast-forward, never before. Fail toward redundant work, never toward
        # missing code.
        await self._record(id, Actor.IMPLEMENTER, EventKind.SUB_ISSUE_CLOSED, SubIssueState.LANDED)
        # Nothing is thrown away here: the fast-forward put these commits on the integration branch.
        self._git.discard_worktree(candidate.worktree)
        return _Closed(id, None)

    # TODO: Editor shall write 'Findings' in spec, which is specified in UL.
    async def _adjudicate(
        self,
        context: SessionContext,
        report: FailureReport,
    ) -> _Closed | None:
        """The Editor half of the cycle: read the failure, return a verdict, and act on it.

        **The Editor never writes code.** It reads the spec, the findings, the failure report and
        the failed worktree, and it returns a judgment. Every *consequence* of that judgment —
        storing the revision, writing the run log, spending the cycle, refusing a fourth — happens
        here, in the scheduler, and nowhere else. The port takes no `RunLog` and no `IssueStore` on
        purpose.
        """
        candidate = context.candidate
        id = candidate.id
        # A cycle is spent the moment an Implementer failure routes to the Editor: that is when the
        # Editor half begins. Spent *before* the session, so that a killed Editor still costs one —
        # otherwise an Editor that reliably times out would buy a sub-issue infinite Implementers,
        # and the cap would hold only along the paths that were working anyway.
        self._ledger.spend(id)
        must_be_terminal = self._ledger.must_be_terminal(id)

        await self._record(id, Actor.EDITOR, EventKind.SESSION_STARTED, SubIssueState.IN_PROGRESS)
        telemetry, verdict = await self._editor.adjudicate(context, report, must_be_terminal)
        outcome = classify_editor(telemetry, verdict)
        # The Editor session belongs to the cycle its Implementer failure opened, not to a cycle of
        # its own — which is what puts the two halves of one cycle side by side in the transcripts.
        await self._record_session(id, report.cycles, Actor.EDITOR, outcome, telemetry)
        await self._record(id, Actor.EDITOR, EventKind.SESSION_FINISHED, outcome)

        if route(Actor.EDITOR, outcome) is Destination.HUMAN or verdict is None:
            # The Editor itself was killed, or came back with nothing. Escalate on the **Editor's**
            # outcome and telemetry: the Implementer's failure is no longer the interesting fact —
            # that the harness cannot adjudicate it is. A human told `impasse` here would go and
            # rewrite a spec, when what actually needs fixing is the Editor.
            #
            # (`verdict is None` is already covered by the route above: `classify_editor` calls a
            # verdictless Editor `infra-failed`, which routes to the human. It is restated only
            # because the type-checker cannot read the taxonomy.)
            failed = failure_report(outcome, telemetry, report.suite, None, report.cycles)
            return await self._quarantine(candidate, Actor.EDITOR, failed)

        await self._record(id, Actor.EDITOR, EventKind.VERDICT_RECORDED, verdict.verdict)

        if verdict.verdict.is_terminal:
            # `planning-defect` — the spec cannot be satisfied as written, and rewriting it is a
            # Planner's call, not an Editor's. `inconclusive` — the Editor could not tell. Both are
            # terminal: another Implementer session would be a coin flip we have already paid for.
            return await self._quarantine(candidate, Actor.EDITOR, report)

        if must_be_terminal:
            # **The scheduler rejects it, and only the scheduler.** The Editor was told this was the
            # final cycle and asked for another anyway. Its verdict is refused rather than obeyed:
            # the cap is the harness's rule, and a rule enforced by asking a model nicely is not a
            # rule. The sub-issue escalates on the Implementer's failure, which is what a human
            # needs to see.
            log.warning(
                "%s: the Editor returned `revise` on its final cycle; refusing a fourth "
                "Implementer session. Escalating to a human.",
                id,
            )
            return await self._quarantine(candidate, Actor.EDITOR, report)

        revised_spec, revised_findings = verdict.revision
        # Knowledge survives **only** through the findings. Nothing else crosses: the diff is
        # discarded, the transcript is discarded, and what the Editor chose to write down is all the
        # next session gets. That choice is the Editor's judgment, unmandated — and keeping it out
        # of the spec is what lets a session be *helped* without the bar being *lowered*.
        await self._store.record_revision(
            id,
            revised_spec,
            revised_findings if revised_findings is not None else candidate.findings,
        )
        self._git.discard_worktree(candidate.worktree)
        return None

    async def _record_reconciliation(self, id: SubIssueId, cycle: int, land: Land) -> None:
        """The gate dispatched the Integrator; the scheduler writes down that it happened.

        Both events, in order, whatever the landing did next: a session that spent tokens is billed
        even when it succeeded and the sub-issue went on to land as if nothing had happened.

        **Stamped from the session's own wall clock, not from the moment this runs.** The gate
        holds the merge lock for the whole reconciliation, so the scheduler only hears about that
        session once it is over and both events would otherwise carry the same instant — a
        two-minute Integrator recorded as having started and finished in the same millisecond,
        which is indistinguishable in the log from one that crashed on startup.
        """
        if land.integrator is None:
            return
        assert land.integrator_outcome is not None
        finished = datetime.now(UTC)
        started = finished - timedelta(seconds=land.integrator.wall_clock_s)
        await self._record(
            id, Actor.INTEGRATOR, EventKind.SESSION_STARTED, SubIssueState.IN_PROGRESS, started
        )
        await self._record_session(
            id,
            cycle,
            Actor.INTEGRATOR,
            land.integrator_outcome,
            land.integrator,
            merge_finished=land.merge_finished,
        )
        await self._record(
            id, Actor.INTEGRATOR, EventKind.SESSION_FINISHED, land.integrator_outcome, finished
        )

    async def _quarantine(
        self, candidate: Candidate, actor: Actor, report: FailureReport
    ) -> _Closed:
        """The worktree is the evidence, and evidence is only preserved if it can be found.

        Raises if the destination is occupied — which means a previous run's wreckage for this same
        sub-issue is still sitting there un-triaged, and the sub-issue was authorised `ready` again
        anyway. That is worth stopping for; quietly clobbering it is not.
        """
        id = candidate.id
        self._git.move_worktree(candidate.worktree, self._worktree_dir(QUARANTINE, id))
        await self._record(id, actor, EventKind.SUB_ISSUE_CLOSED, SubIssueState.NEEDS_HUMAN)
        return _Closed(id, report)

    async def _record_session(
        self,
        id: SubIssueId,
        cycle: int,
        actor: Actor,
        outcome: Outcome,
        telemetry: SessionTelemetry,
        merge_finished: bool | None = None,
    ) -> None:
        """What a finished session is billed for, and what it said — for all three actors alike.

        The two travel together because they are one fact: this session happened, here is what it
        cost and here is what it produced. Recorded from the scheduler even for the Integrator,
        whose session the merge gate dispatched: the gate writes nothing, deliberately, and
        `Land.integrator` is how its telemetry reaches a writer.
        """
        await self._store.record_consumption(
            id,
            SessionConsumption(
                actor=actor,
                consumption=telemetry.consumption,
                auto_compactions=telemetry.auto_compactions,
            ),
        )
        await self._transcripts.write(
            FinishedSession(
                sub_issue=id,
                cycle=cycle,
                actor=actor,
                outcome=outcome,
                telemetry=telemetry,
                budget=self._budget,
                merge_finished=merge_finished,
            )
        )

    async def _record(
        self,
        id: SubIssueId,
        actor: Actor,
        kind: EventKind,
        details: Outcome | Verdict | SubIssueState,
        at: datetime | None = None,
    ) -> None:
        """Two sinks, different durability. The run log is **authoritative** — failing to write it
        fails the run. The issue store is **best-effort**: it mirrors the transition back into the
        tracker for a human's benefit, and a run must not die because the tracker was unreachable
        (a read-only file today; Linear being down tomorrow).
        """
        e = event(id, actor, kind, details, at)
        await self._run_log.write(e)
        try:
            await self._store.write_event(e)
        except Exception:  # noqa: BLE001 — best-effort by contract; the run log is the record
            log.warning(
                "could not mirror %s into the issue store; the run log stands", e, exc_info=True
            )
