"""The Integrator role core: everything about running an Integrator that is not specific to one
concrete adapter.

The third of the trio, and the thinnest. `implementer.py` collects the `<impasse>` sentinel and a
commit count; `editor.py` parses a verdict and reports zero commits by construction. An Integrator
declares nothing and is asked nothing: the merge queue reads the answer off git afterwards, so this
module only has to carry the session's cost and its transcript.

`commits` and `diffstat` are measured here anyway, against the worktree's base, because they are
what a human reads in the notification when a reconciliation fails — but nothing is *classified*
from them. `classify_integrator` asks git whether the merge is finished, for the reason recorded
there.
"""

from __future__ import annotations

from ralph.adapters.git import run_git
from ralph.adapters.runtime.bounding import Bound
from ralph.adapters.runtime.turn_stream import TurnStreamSession, run_turn_stream
from ralph.harness import SessionTelemetry
from ralph.ports import SessionContext, Worktree


def integrator_telemetry(
    *,
    bound: Bound,
    exit_code: int,
    output: str,
    wall_clock_s: float,
    worktree: Worktree,
    auto_compactions: int = 0,
) -> SessionTelemetry:
    """The harness facts every Integrator session reports, regardless of transport."""
    return SessionTelemetry(
        exit_code=exit_code,
        killed=bound.killed,
        consumption=bound.consumption,
        auto_compactions=auto_compactions,
        # Nothing resumes an Integrator: it is dispatched once, inside the merge lock, and the
        # queue has released that lock by the time anyone could ask.
        resumable_identifier=None,
        wall_clock_s=wall_clock_s,
        commits=int(run_git(worktree.path, "rev-list", "--count", f"{worktree.base}..HEAD")),
        diffstat=run_git(worktree.path, "diff", "--stat", f"{worktree.base}..HEAD"),
        session_output=output,
        impasse_report=None,  # an Integrator is not asked to satisfy a spec, so it cannot fail to.
    )


async def run_turn_stream_integrator(
    session: TurnStreamSession, context: SessionContext
) -> SessionTelemetry:
    """One SDK-backed Integrator session, plus the harness-owned git facts."""
    completed = await run_turn_stream(session, context.budget)
    return integrator_telemetry(
        bound=completed.bound,
        exit_code=completed.exit_code,
        output=completed.output,
        wall_clock_s=completed.wall_clock_s,
        worktree=context.candidate.worktree,
        auto_compactions=completed.auto_compactions,
    )
