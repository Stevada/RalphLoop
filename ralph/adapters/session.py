"""Running an agent in a worktree, and collecting the facts it cannot report about itself.

**This is the whole of an Implementer that is not model-specific.** Codex and Copilot are this
plus an argv and a context source; the stand-in agent is this plus an argv. Everything that makes
a session a session — the wall-clock bound, counting the commits, reading the diffstat, finding
the `<impasse>` sentinel — happens here, once.

The model's exit code is its opinion. Everything in the `SessionTelemetry` this returns is the
harness's own observation, and the two are allowed to disagree. That disagreement is the signal.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ralph.adapters.git import run_git
from ralph.domain import Approach, Brief, Findings, ImpasseReport, SessionTelemetry
from ralph.ports import Budget, Worktree

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"

BuildArgv = Callable[[Brief, Findings, Worktree], Sequence[str]]
"""What separates one Implementer from another, in this ticket: how you spell the command."""


class ImpasseParseError(ValueError):
    """The session emitted a sentinel the harness cannot read.

    Loud, and deliberately so. An unreadable impasse would otherwise be classified `silent-red` —
    routing a session that *told us it was stuck* to an Editor as though it had lied about
    succeeding. The failure would look exactly like a correctly-handled one.
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


async def run_agent(argv: Sequence[str], wt: Worktree, budget: Budget) -> SessionTelemetry:
    """One session. Bounded on the wall clock; the context ceiling arrives with #08, which is
    where there is a live context signal to meter.

    The two bounds catch different failures. A session spinning on a failing suite has a *flat*
    context and would never trip a ceiling — only the clock stops it.
    """
    started = time.monotonic()
    killed: Literal["ceiling", "wall-clock"] | None = None

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=wt.path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        async with asyncio.timeout(budget.wall_clock_s):
            out, _ = await proc.communicate()
    except TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()  # whatever it managed to say before we stopped it
        killed = "wall-clock"

    output = out.decode(errors="replace")
    return SessionTelemetry(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        killed=killed,
        peak_context_tokens=0,  # no context source yet — #08 meters this
        consumed_tokens=0,
        wall_clock_s=time.monotonic() - started,
        commits=int(run_git(wt.path, "rev-list", "--count", f"{wt.base}..HEAD")),
        diffstat=run_git(wt.path, "diff", "--stat", f"{wt.base}..HEAD"),
        session_output=output,
        impasse_report=parse_impasse(output),
    )


@dataclass(frozen=True, slots=True)
class SubprocessImplementer:
    """An Implementer is an argv and a worktree. That is the insight this ticket buys."""

    build_argv: BuildArgv

    async def run(
        self, brief: Brief, findings: Findings, worktree: Worktree, budget: Budget
    ) -> SessionTelemetry:
        return await run_agent(self.build_argv(brief, findings, worktree), worktree, budget)
