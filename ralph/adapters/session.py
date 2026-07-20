"""Running an Implementer in a worktree, and collecting the facts it cannot report about itself.

**This is the whole of a subprocess Implementer that is not model-specific.** Codex is this plus an
argv and final usage; the stand-in Implementer is this plus an argv. Everything that makes a
subprocess session a session — bounding on the clock, counting the commits, reading the diffstat,
finding the `<impasse>` sentinel — happens here, once.

The model's exit code is its opinion. Everything in the `SessionTelemetry` this returns is the
harness's own observation, and the two are allowed to disagree. That disagreement is the signal.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ralph.adapters.context import Bound, run_bounded
from ralph.adapters.git import run_git
from ralph.adapters.turn_stream import TurnStreamSession, run_turn_stream
from ralph.harness import Approach, ImpasseReport, SessionTelemetry
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext, Worktree

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"

BuildArgv = Callable[[Brief, Findings, Worktree], Sequence[str]]
"""What separates one Implementer from another: how you spell the command."""


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


class Transcript:
    """The session's stdout, accumulated in full for telemetry."""

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self.closed = asyncio.Event()
        """Set when stdout reaches EOF — the session has said everything it is going to say."""

    def append(self, line: str) -> None:
        self._chunks.append(line)

    def close(self) -> None:
        self.closed.set()

    @property
    def text(self) -> str:
        return "".join(self._chunks)


async def _pump(stream: asyncio.StreamReader, transcript: Transcript) -> None:
    async for line in stream:
        transcript.append(line.decode(errors="replace"))
    transcript.close()


@dataclass(frozen=True, slots=True)
class Session:
    """What the harness observed of a subprocess session, before anyone asks what role it played.

    An Implementer session and a Copilot Editor session are the same event at this level — a
    command, in a directory, under both bounds — and they differ only in what is read out of the
    output afterwards: an `<impasse>` and a commit count, or a `<verdict>` and nothing.
    """

    bound: Bound
    exit_code: int
    output: str
    wall_clock_s: float


FinalConsumedTokens = Callable[[Session, Worktree], Awaitable[int | None]]
"""Where a model-specific adapter reads the session's final consumption figure, if it publishes
one. `None` means there was no final figure to read, and the live observations remain the fallback."""


async def run_session(argv: Sequence[str], cwd: Path, budget: Budget) -> Session:
    """Run a command under the wall-clock bound and collect everything it said."""
    started = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    if proc.stdout is None:  # pragma: no cover — PIPE was asked for above
        raise RuntimeError("the session has no stdout to read")

    transcript = Transcript()
    pump = asyncio.create_task(_pump(proc.stdout, transcript))
    bound = await run_bounded(proc, budget)
    await pump  # the process is dead; drain whatever it managed to say before we stopped it

    return Session(
        bound=bound,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        output=transcript.text,
        wall_clock_s=time.monotonic() - started,
    )


def implementer_telemetry(
    *,
    bound: Bound,
    exit_code: int,
    output: str,
    wall_clock_s: float,
    worktree: Worktree,
) -> SessionTelemetry:
    """The harness facts every Implementer session reports, regardless of transport."""
    return SessionTelemetry(
        exit_code=exit_code,
        killed=bound.killed,
        consumed_tokens=bound.consumed_tokens,
        wall_clock_s=wall_clock_s,
        commits=int(run_git(worktree.path, "rev-list", "--count", f"{worktree.base}..HEAD")),
        diffstat=run_git(worktree.path, "diff", "--stat", f"{worktree.base}..HEAD"),
        session_output=output,
        impasse_report=parse_impasse(output),
    )


async def run_agent(
    argv: Sequence[str],
    wt: Worktree,
    budget: Budget,
    final_consumed_tokens: FinalConsumedTokens | None = None,
) -> SessionTelemetry:
    """One Implementer session: a bounded subprocess, plus the two facts it cannot report about
    itself — how many commits it actually made, and what it actually changed."""
    session = await run_session(argv, wt.path, budget)
    consumed_tokens = session.bound.consumed_tokens
    if final_consumed_tokens is not None:
        final = await final_consumed_tokens(session, wt)
        if final is not None:
            consumed_tokens = final
    return implementer_telemetry(
        bound=Bound(killed=session.bound.killed, consumed_tokens=consumed_tokens),
        exit_code=session.exit_code,
        output=session.output,
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
        wall_clock_s=completed.wall_clock_s,
        worktree=context.worktree,
    )


@dataclass(frozen=True, slots=True)
class SubprocessImplementer:
    """An Implementer is an argv, a worktree, and the telemetry its CLI publishes.

    That is the whole of it for subprocess-backed actors. Codex is this with `codex exec` and
    end-of-turn usage; the stand-in Implementer is this with neither. Nothing above this line knows
    the difference.
    """

    build_argv: BuildArgv
    final_consumed_tokens: FinalConsumedTokens | None = None

    async def run(self, context: SessionContext) -> SessionTelemetry:
        worktree = context.worktree
        return await run_agent(
            self.build_argv(context.brief, context.findings, worktree),
            worktree,
            context.budget,
            self.final_consumed_tokens,
        )
