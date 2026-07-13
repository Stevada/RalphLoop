"""The run: read the graph, refuse a red base, cut a worktree, run a session, land it.

**Thin on purpose.** No concurrency (#05), no quarantine-and-drain (#06), no Editor (#07), no
context ceiling (#08). Each of those has a ticket, and each one arrives into a system that already
runs. What is here is the whole path, end to end, and nothing beside it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ralph.domain import (
    EventKind,
    Outcome,
    SubIssue,
    SubIssueId,
    SubIssueState,
    classify_implementer,
    eligible,
)
from ralph.events import event
from ralph.mergequeue import LandResult, MergeQueue
from ralph.ports import Budget, Git, Implementer, IssueStore, RunLog, TestRunner

WORKTREES = Path(".worktrees") / "active"

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
    landed: tuple[SubIssueId, ...]
    failed: Mapping[SubIssueId, Outcome]

    @property
    def clean(self) -> bool:
        return not self.failed


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

    async def run(self) -> RunReport:
        await self._refuse_a_red_base()

        graph, states = self._store.read_graph()
        landed: list[SubIssueId] = []
        failed: dict[SubIssueId, Outcome] = {}

        # Sequential, one at a time, in id order. Parallelism is #05 — and the merge queue below is
        # already built for it, which is the point of putting it here rather than there.
        while ready := sorted(eligible(graph, states) - failed.keys()):
            id = ready[0]
            outcome = await self._pipeline(graph.sub_issues[id])
            if outcome is Outcome.SUCCESS:
                states[id] = SubIssueState.LANDED
                landed.append(id)
            else:
                # It stays IN_PROGRESS and never becomes eligible again. Quarantine — marking it
                # `needs-human`, preserving its worktree, draining the rest — is #06.
                failed[id] = outcome

        return RunReport(landed=tuple(landed), failed=failed)

    async def _refuse_a_red_base(self) -> None:
        base = await self._runner.run(self._repo)
        if not base.green:
            raise BaseIsRed(
                f"the suite is red on {self._integration} before any session has started. "
                f"No agent will be dispatched into a broken base.\n\n{base.output}"
            )

    async def _pipeline(self, sub: SubIssue) -> Outcome:
        """One sub-issue, one session. What makes it a *cycle* — the Editor — arrives in #07."""
        await self._record(sub.id, "session-opened", SubIssueState.IN_PROGRESS)

        wt = self._git.add_worktree(
            f"ralph/{sub.id}", self._repo / WORKTREES / str(sub.id), self._integration
        )
        brief, findings = self._store.content(sub.id)

        telemetry = await self._implementer.run(brief, findings, wt, self._budget)
        # The suite result, not the exit code, is the outcome. The harness runs the tests.
        outcome = classify_implementer(telemetry, await self._runner.run(wt.path))

        if outcome is Outcome.SUCCESS:
            if await self._merge_queue.land(sub, wt) is not LandResult.LANDED:
                # Green in isolation, and it will not integrate. The Implementer could not have
                # observed this about itself — which is why it goes to the Editor rather than back
                # to the actor that produced it. In #07, when there is one.
                outcome = Outcome.INTEGRATION_FAILED

        await self._record(sub.id, "session-closed", outcome)
        if outcome is Outcome.SUCCESS:
            # After the fast-forward, never before. Fail toward redundant work, never toward
            # missing code.
            await self._record(sub.id, "terminal", SubIssueState.LANDED)
        return outcome

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
