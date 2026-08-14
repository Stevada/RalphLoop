"""The Implementer role core: everything about running an Implementer that is not specific to one
concrete adapter.

Two transports feed it. A subprocess Implementer (Codex, the stand-in) runs through
`run_subprocess_implementer` over the `Session` in `session.py`; an SDK Implementer (Copilot,
Codex-over-SDK) runs through
`run_turn_stream_implementer` over the `TurnStreamSession` in `turn_stream.py`. Either way the same
facts are collected here, once — the `<impasse>` sentinel, the commit count, the diffstat.

This is the twin of `editor.py`. The model's exit code is its opinion; everything in the
`SessionTelemetry` returned here is the harness's own observation, and the two are allowed to
disagree. That disagreement is the signal.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ralph.adapters.runtime.bounding import Bound
from ralph.adapters.git import run_git
from ralph.adapters.runtime.session import Session, run_session
from ralph.adapters.runtime.turn_stream import TurnStreamSession, run_turn_stream
from ralph.harness import (
    NOTHING,
    Approach,
    ImpasseReport,
    SessionTelemetry,
    TokenConsumption,
)
from ralph.issues import Findings, Spec
from ralph.ports import Budget, SessionContext, Worktree

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"

BuildArgv = Callable[[Spec, Findings, Worktree], Sequence[str]]
"""What separates one Implementer from another: how you spell the command."""

BuildResolveArgv = Callable[[SessionContext, str], Sequence[str] | None]
"""How a subprocess-backed Implementer resumes, if that transport has a way to do it."""


class ImpasseParseError(ValueError):
    """The session emitted a sentinel the harness cannot read.

    Loud, and deliberately so. An unreadable impasse would otherwise be classified as an undeclared
    impasse — the model's claim silently dropped, the failure looking exactly like a correctly-handled
    one.
    """


def parse_impasse(output: str) -> ImpasseReport | None:
    """The sentinel, or nothing. The body is JSON keyed to `ImpasseReport`'s own fields."""
    start = output.find(IMPASSE_OPEN)
    if start == -1:
        return None
    end = output.find(IMPASSE_CLOSE, start)
    if end == -1:
        raise ImpasseParseError(f"{IMPASSE_OPEN} with no {IMPASSE_CLOSE}")

    body = output[start + len(IMPASSE_OPEN) : end]
    try:
        raw = json.loads(body)
        return ImpasseReport(
            failing_test=raw["failing_test"],
            assertion_output=raw["assertion_output"],
            approaches=tuple(
                Approach(tried=a["tried"], abandoned_because=a["abandoned_because"])
                for a in raw["approaches"]
            ),
            unsatisfiable_criterion=raw["unsatisfiable_criterion"],
            what_would_satisfy=raw["what_would_satisfy"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ImpasseParseError(f"unreadable impasse report: {body!r}") from exc


FinalConsumption = Callable[[Session, Worktree], Awaitable[TokenConsumption | None]]
"""Where a model-specific adapter reads the session's final consumption figure, if it publishes
one. `None` means there was no final figure to read, and the live observations remain the fallback."""


def implementer_telemetry(
    *,
    bound: Bound,
    exit_code: int,
    output: str,
    transcript: str,
    wall_clock_s: float,
    worktree: Worktree,
    auto_compactions: int = 0,
    resumable_identifier: str | None = None,
) -> SessionTelemetry:
    """The harness facts every Implementer session reports, regardless of transport."""
    return SessionTelemetry(
        exit_code=exit_code,
        killed=bound.killed,
        consumption=bound.consumption,
        auto_compactions=auto_compactions,
        resumable_identifier=resumable_identifier,
        wall_clock_s=wall_clock_s,
        commits=int(run_git(worktree.path, "rev-list", "--count", f"{worktree.base}..HEAD")),
        diffstat=run_git(worktree.path, "diff", "--stat", f"{worktree.base}..HEAD"),
        session_output=output,
        impasse_report=parse_impasse(output),
        transcript=transcript,
    )


async def run_subprocess_implementer(
    argv: Sequence[str],
    wt: Worktree,
    budget: Budget,
    final_consumption: FinalConsumption | None = None,
) -> SessionTelemetry:
    """One Implementer session: a bounded subprocess, plus the two facts it cannot report about
    itself — how many commits it actually made, and what it actually changed."""
    session = await run_session(argv, wt.path, budget)
    consumption = session.bound.consumption
    if final_consumption is not None:
        final = await final_consumption(session, wt)
        if final is not None:
            consumption = final
    return implementer_telemetry(
        bound=Bound(killed=session.bound.killed, consumption=consumption),
        exit_code=session.exit_code,
        output=session.output,
        transcript=session.transcript,
        wall_clock_s=session.wall_clock_s,
        worktree=wt,
    )


async def run_turn_stream_implementer(
    session: TurnStreamSession, context: SessionContext
) -> SessionTelemetry:
    """One SDK-backed Implementer session, plus the harness-owned git facts."""
    completed = await run_turn_stream(session, context.budget)
    return implementer_telemetry(
        bound=completed.bound,
        exit_code=completed.exit_code,
        output=completed.output,
        transcript=completed.transcript,
        wall_clock_s=completed.wall_clock_s,
        worktree=context.candidate.worktree,
        auto_compactions=completed.auto_compactions,
        resumable_identifier=completed.resumable_identifier,
    )


@dataclass(frozen=True, slots=True)
class SubprocessImplementer:
    """An Implementer is an argv, a worktree, and the telemetry its CLI publishes.

    That is the whole of it for subprocess-backed actors. Codex is this with `codex exec` and
    end-of-turn usage; the stand-in Implementer is this with neither. Nothing above this line knows
    the difference.
    """

    build_argv: BuildArgv
    final_consumption: FinalConsumption | None = None
    build_resolve_argv: BuildResolveArgv | None = None

    async def run(self, context: SessionContext) -> SessionTelemetry:
        worktree = context.candidate.worktree
        return await run_subprocess_implementer(
            self.build_argv(context.candidate.spec, context.candidate.findings, worktree),
            worktree,
            context.budget,
            self.final_consumption,
        )

    async def resolve_conflict(
        self, context: SessionContext, resumable_identifier: str
    ) -> SessionTelemetry:
        if self.build_resolve_argv is None:
            return _resume_unavailable(context.candidate.worktree, resumable_identifier)
        argv = self.build_resolve_argv(context, resumable_identifier)
        if argv is None:
            return _resume_unavailable(context.candidate.worktree, resumable_identifier)
        return await run_subprocess_implementer(
            argv,
            context.candidate.worktree,
            context.budget,
            self.final_consumption,
        )


def _resume_unavailable(wt: Worktree, resumable_identifier: str) -> SessionTelemetry:
    said = f"cannot resume subprocess Implementer session {resumable_identifier}"
    return implementer_telemetry(
        bound=Bound(killed=None, consumption=NOTHING),
        exit_code=124,
        output=said,
        transcript=said,  # no session ran, so the harness's own sentence is the whole account
        wall_clock_s=0.0,
        worktree=wt,
        resumable_identifier=resumable_identifier,
    )
