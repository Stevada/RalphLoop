"""Running an agent in a worktree, and collecting the facts it cannot report about itself.

**This is the whole of an Implementer that is not model-specific.** Codex and Copilot are this
plus an argv and a context source; the stand-in agent is this plus an argv. Everything that makes
a session a session — both bounds, counting the commits, reading the diffstat, finding the
`<impasse>` sentinel — happens here, once.

The model's exit code is its opinion. Everything in the `SessionTelemetry` this returns is the
harness's own observation, and the two are allowed to disagree. That disagreement is the signal.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ralph.adapters.context import Bound, run_bounded
from ralph.adapters.git import run_git
from ralph.harness import Approach, ImpasseReport, SessionTelemetry
from ralph.issues import Brief, Findings
from ralph.ports import Budget, ContextSource, SessionContext, Worktree

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"

BuildArgv = Callable[[Brief, Findings, Worktree], Sequence[str]]
"""What separates one Implementer from another, in this ticket: how you spell the command."""


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
    """The session's stdout: accumulated in full for telemetry, and republished line by line while
    it is still arriving.

    Two consumers with different needs, and pretending they were one is what would go wrong.
    Telemetry wants the whole transcript, at the end, in order — that is `text`. A `ContextSource`
    wants **one fact, live**: Codex announces its `thread_id` on the first line of stdout and that
    id is the only link between this process and its rollout file. So `first()` watches the stream
    until it finds what it came for and then stops republishing; nothing accumulates behind a
    consumer that has lost interest.
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._live: asyncio.Queue[str | None] | None = asyncio.Queue()
        self.closed = asyncio.Event()
        """Set when stdout reaches EOF — the session has said everything it is going to say. This
        is a `ContextSource`'s signal to stop tailing, and it fires slightly *before* the process
        exits, which is why the tail reads once more afterwards."""

    def append(self, line: str) -> None:
        self._chunks.append(line)
        if self._live is not None:
            self._live.put_nowait(line)

    def close(self) -> None:
        if self._live is not None:
            self._live.put_nowait(None)
        self.closed.set()

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    async def first(self, extract: Callable[[str], str | None]) -> str | None:
        """The first thing `extract` finds on a line of stdout, or None if the session ended
        without ever saying it. Watchable once: there is only one such fact, and only one asker."""
        live = self._live
        if live is None:
            raise RuntimeError("stdout is watchable once, and something is already watching it")
        try:
            while (line := await live.get()) is not None:
                found = extract(line)
                if found is not None:
                    return found
            return None
        finally:
            self._live = None  # stop republishing; `text` keeps accumulating regardless


BoundSource = Callable[[Transcript], ContextSource]
"""A context source with everything it needs but the session's own voice."""

SourceFactory = Callable[[Transcript, Worktree], ContextSource]
"""How an Implementer finds its own context signal. Both CLIs publish it to a file, and neither
puts the number on stdout — but they answer *which file is mine?* differently, and the two answers
are why this takes both arguments. Codex writes into one shared sessions directory and must be
matched to its own rollout by the thread id it announces on **stdout**. Copilot is handed a private
`--log-dir` derived from its **worktree**, and so has nothing to disambiguate at all."""


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


async def run_session(
    argv: Sequence[str], cwd: Path, budget: Budget, context: BoundSource | None = None
) -> Session:
    """Run a command under both bounds and collect everything it said.

    `context=None` is an agent with no context signal — the stand-in, or a bare `RALPH_AGENT_CMD`.
    It runs on the clock alone and reports a peak of zero, which is the truth: nobody was watching.
    """
    started = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    if proc.stdout is None:  # pragma: no cover — PIPE was asked for above
        raise RuntimeError("the session has no stdout to read")

    transcript = Transcript()
    pump = asyncio.create_task(_pump(proc.stdout, transcript))
    bound = await run_bounded(proc, context(transcript) if context is not None else None, budget)
    await pump  # the process is dead; drain whatever it managed to say before we stopped it

    return Session(
        bound=bound,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        output=transcript.text,
        wall_clock_s=time.monotonic() - started,
    )


async def run_agent(
    argv: Sequence[str], wt: Worktree, budget: Budget, context: BoundSource | None = None
) -> SessionTelemetry:
    """One Implementer session: a bounded subprocess, plus the two facts it cannot report about
    itself — how many commits it actually made, and what it actually changed."""
    session = await run_session(argv, wt.path, budget, context)
    return SessionTelemetry(
        exit_code=session.exit_code,
        killed=session.bound.killed,
        peak_context_tokens=session.bound.peak_context_tokens,
        consumed_tokens=session.bound.consumed_tokens,
        wall_clock_s=session.wall_clock_s,
        commits=int(run_git(wt.path, "rev-list", "--count", f"{wt.base}..HEAD")),
        diffstat=run_git(wt.path, "diff", "--stat", f"{wt.base}..HEAD"),
        session_output=session.output,
        impasse_report=parse_impasse(session.output),
    )


@dataclass(frozen=True, slots=True)
class SubprocessImplementer:
    """An Implementer is an argv, a worktree, and — where the CLI publishes one — a context source.

    That is the whole of it. Codex is this with `codex exec` and a rollout tail; Copilot is this
    with `copilot -p` and a debug-log tail; the stand-in agent is this with neither. Nothing above
    this line knows the difference.
    """

    build_argv: BuildArgv
    context: SourceFactory | None = None

    async def run(self, context: SessionContext) -> SessionTelemetry:
        factory = self.context
        worktree = context.worktree
        bound: BoundSource | None = (
            None if factory is None else lambda transcript: factory(transcript, worktree)
        )
        return await run_agent(
            self.build_argv(context.brief, context.findings, worktree),
            worktree,
            context.budget,
            bound,
        )
