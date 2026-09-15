"""How a run is told to a human: the terminal narration and the final notification.

Values in, strings out. The one exception is `NarratedRunLog`, which is here because narrating is
the whole of what it adds to the `RunLog` it wraps.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from ralph.harness import CycleLedger, FailureReport, Refusal, TokenConsumption
from ralph.notification import Notification
from ralph.ports import RepoCommands, RunLog
from ralph.runlog import Event


@dataclass(frozen=True, slots=True)
class Readiness:
    """The pre-flight result plus the commands it discovered, when discovery succeeded."""

    refusals: tuple[Refusal, ...]
    commands: RepoCommands | None


def render_refusals(found: tuple[Refusal, ...]) -> str:
    if not found:
        return "ready to run."
    lines = [f"refusing to run ({len(found)}):"]
    lines += [f"\n  {r.check.value}\n    {r.reason}" for r in found]
    return "\n".join(lines)


def render_readiness(result: Readiness) -> str:
    if result.refusals:
        return render_refusals(result.refusals)
    lines = [
        "ready to run.",
        "commands:",
        f"  test: {shlex.join(result.commands.test if result.commands is not None else ())}",
    ]
    if result.commands is not None and result.commands.install is not None:
        lines.append(f"  install: {shlex.join(result.commands.install)}")
    return "\n".join(lines)


def render_event(e: Event) -> str:
    """One run-log event, for a human watching it happen.

    The same five fields the JSONL line carries, in the same order — this is a *view* of the record,
    not a second record. UTC like the file, and time-of-day only: a run is hours, not days, and the
    date would be five characters of noise on every line.
    """
    return (
        f"{e.ts:%H:%M:%S}  {e.sub_issue:<10} {e.actor.value:<12} "
        f"{e.kind.value:<17} {e.details.value}"
    )


@dataclass(frozen=True, slots=True)
class NarratedRunLog:
    """A `RunLog` that also narrates to the terminal as it writes.

    The file is the record; this is the only account of a run *while it is still happening*. The
    notification comes at the end, which is hours too late to tell you a wave has started, and
    `tail -f` on a path the run computes for itself is a poor substitute for the run saying so.

    Ordered file-first on purpose: the terminal must never claim something the record does not.
    """

    inner: RunLog

    async def write(self, e: Event) -> None:
        await self.inner.write(e)
        # Unbuffered, because the whole value here is timeliness — a narration that arrives in a
        # 4KB block when the run ends is the notification again, with worse formatting.
        print(render_event(e), flush=True)

    def events(self) -> tuple[Event, ...]:
        return self.inner.events()


def render_consumption(c: TokenConsumption) -> str:
    """The total, and the breakdown where there is one.

    A vendor that reported only a total gets to have said only that. Printing three zeros beside a
    real total would read as a session that generated nothing, which is a different claim entirely.
    """
    if c.input_tokens is None or c.cache_read_tokens is None or c.output_tokens is None:
        return f"{c.consumed_tokens} tokens (no breakdown)"
    return (
        f"{c.consumed_tokens} tokens "
        f"({c.input_tokens} in, {c.cache_read_tokens} cached, {c.output_tokens} out)"
    )


def render(n: Notification, parent_issue_name: str | None = None) -> str:
    """**One** notification, at the end.

    The bar: *if you cannot tell from this alone whether to spend your first ten minutes reading a
    diff or rewriting a PRD, it has failed.* So each escalation leads with what kind of failure it
    was, says what it is holding up, and — where the model left one — quotes the criterion it
    believes it cannot satisfy. Most urgent first; there is no scrolling to find the important one.
    """
    lines = [f"landed: {', '.join(n.landed) if n.landed else 'nothing'}"]
    if n.consumption:
        lines.append("\nconsumption:")
        for c in n.consumption:
            lines.append(
                f"  {c.sub_issue}: {render_consumption(c.consumption)}, "
                f"{c.auto_compactions} auto-compactions"
            )
        lines.append(
            f"  total: {render_consumption(n.total_consumption)}, "
            f"{n.total_auto_compactions} auto-compactions"
        )
    if not n.escalations:
        return "\n".join(lines)

    lines.append(f"\nneeds a human ({len(n.escalations)}), most urgent first:")
    for e in n.escalations:
        # The cycle count only earns a line when it is not 1. A sub-issue the Editor rewrote twice
        # and which still failed is a different animal from one that failed on first contact, and
        # the difference should be visible without opening the run log.
        spent = (
            f"  (cycle {e.report.cycles} of {CycleLedger.MAX_CYCLES})"
            if e.report.cycles > 1
            else ""
        )
        lines.append(f"\n  {e.sub_issue}  {e.outcome.value}{spent}")
        if e.stranded:
            lines.append(f"    holding up: {', '.join(e.stranded)}")
        if e.report.claim is not None:
            lines.append(f"    it says: {e.report.claim.unsatisfiable_criterion}")
            lines.append(f"    would need: {e.report.claim.what_would_satisfy}")
        if e.report.integration_detail is not None:
            lines.append(f"    the merge gate: {e.report.integration_detail}")
        commits = e.report.telemetry.commits
        worktree_dir = (
            f".worktrees/failed/{parent_issue_name}/{e.sub_issue}"
            if parent_issue_name is not None
            else f".worktrees/failed/{e.sub_issue}"
        )
        lines.append(
            f"    harness: {commits} commit{'' if commits == 1 else 's'}, "
            f"suite {_suite_summary(e.report)}, "
            f"worktree preserved at {worktree_dir}"
        )
    return "\n".join(lines)


def _suite_summary(report: FailureReport) -> str:
    if report.suite is None:
        return "not run"
    return "green" if report.suite.green else "red"
