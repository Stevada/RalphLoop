"""A bounded subprocess session, and the transcript it produced — the subprocess twin of the SDK
`TurnStreamSession` in `turn_stream.py`.

A session is a command, in a directory, under the wall-clock bound. This module knows nothing of
which role runs it: the Implementer role core in `implementer.py` reads an `<impasse>` and a commit
count out of the result. Today only the Implementer runs this way — the Editors are all
turn-stream-backed — but the transport itself is role-neutral by construction.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ralph.adapters.bounding import Bound, run_bounded
from ralph.ports import Budget


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

    A subprocess session is a command, in a directory, under both bounds; what differs by role is
    only what a role core reads out of the output afterwards. Today only the Implementer runs this
    way — the Editors are all turn-stream-backed — but the transport itself knows nothing of that.
    """

    bound: Bound
    exit_code: int
    output: str
    wall_clock_s: float


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
