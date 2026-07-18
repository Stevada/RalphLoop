"""Everything about an Editor that is not model-specific: what it may do, what it must return, and
how it is bounded.

**The moment the Editor commits, it is an Implementer with a different name.** So the read-only
invariant is enforced by the harness — a mutating tool call is *denied*, not discouraged. A prompt
that asks nicely is not an invariant; it is a hope with good manners.

`ClaudeCodeEditor` and `CopilotEditor` are this plus their SDK sessions. What differs is only *how
the denial is delivered*; what counts as mutating is decided once, here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ralph.adapters.context import Bound, run_bounded
from ralph.harness import EditorVerdict, SessionTelemetry, Verdict
from ralph.issues import Brief, Findings
from ralph.ports import Budget

log = logging.getLogger(__name__)

VERDICT_OPEN, VERDICT_CLOSE = "<verdict>", "</verdict>"
"""The Editor's answer, on stdout. Written by `prompt.py`, read here — and spelled out once."""


class VerdictParseError(ValueError):
    """The Editor returned a verdict the harness cannot read."""


# ── what the Editor may do ───────────────────────────────────────────────────────────────────

READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash"})
"""Everything else — `Write`, `Edit`, `NotebookEdit`, and anything an SDK gains next week — is
denied by *absence*. An allowlist fails closed; a blocklist fails open, and it fails open on
precisely the tool nobody thought of."""

READ_ONLY_COMMANDS = frozenset(
    {
        "cat", "cut", "diff", "echo", "file", "find", "grep", "head", "jq", "ls", "pwd",
        "rg", "sort", "stat", "tail", "tr", "tree", "uniq", "wc", "which",
    }
)  # fmt: skip

READ_ONLY_GIT = frozenset(
    {
        "blame", "diff", "grep", "log", "ls-files", "ls-tree", "rev-list", "rev-parse",
        "shortlog", "show", "status",
    }
)  # fmt: skip
"""`commit`, `cherry-pick`, `apply`, `checkout`, `reset`, `push` — and every other git subcommand —
are denied by absence, for the same reason."""

FORBIDDEN_SHELL = ("&", ">", "<", "$(", "`")
"""Redirection, command substitution, backgrounding — and `&&`, which contains `&`.

Each is a way to make an *allowed* command write. `git log > evidence.txt` reads as a `git log`, and
it destroys the failed worktree a human was about to read: the write is in the shell, not in the
program, so a permit that only inspected the program would wave it through.

Pipes survive, because a pipe cannot write by itself — but it can *feed* something that does, so
every command in a pipeline is checked separately. `cat calculator.py | tee copy.py` begins as
innocently as a command can and ends as an Implementer."""

_CHAIN = re.compile(r"\|\||&&|;|\|")


@dataclass(frozen=True, slots=True)
class Permission:
    """Allowed, or denied and why. The `why` goes to the model, so it can try something legal
    rather than concluding the repository is broken."""

    allowed: bool
    reason: str = ""


Permit = Callable[[str, dict[str, object]], Permission]


@dataclass(frozen=True, slots=True)
class Ask:
    """Everything an SDK-backed Editor session needs to start."""

    prompt: str
    cwd: Path
    permit: Permit | None = None


OpenSession = Callable[[Ask], "EditorSession"]


def _permitted_command(argv: Sequence[str], suite: Sequence[str]) -> Permission:
    if not argv:
        return Permission(False, "an empty command")
    if tuple(argv[: len(suite)]) == tuple(suite):
        return Permission(True)  # the Editor may re-run the suite. It is the point of the session.
    head = argv[0]
    if head == "git":
        if len(argv) > 1 and argv[1] in READ_ONLY_GIT:
            return Permission(True)
        sub = argv[1] if len(argv) > 1 else ""
        return Permission(False, f"`git {sub}` may write to the repository. You are read-only.")
    if head in READ_ONLY_COMMANDS:
        return Permission(True)
    return Permission(
        False,
        f"`{head}` is not on the Editor's read-only allowlist. You may read the worktree, grep it, "
        f"and run its suite (`{shlex.join(suite)}`) — nothing else.",
    )


def read_only(tool: str, input: Mapping[str, object], suite: Sequence[str]) -> Permission:
    """**The enforcement surface.** Every tool call the Editor makes comes through here first.

    The Editor's whole value is *reproduction, not inference*: it re-runs the suite, greps for the
    API the Implementer swore did not exist, and checks the story against the repository. Since
    declaring an impasse is cheap, that check is the only thing standing between us and a system
    where declaring an impasse always works. So the Editor must be able to *run things* — which is
    exactly what makes this function necessary, and exactly why it cannot be a blocklist.
    """
    if tool not in READ_ONLY_TOOLS:
        return Permission(False, f"`{tool}` can modify the worktree. The Editor is read-only.")
    if tool != "Bash":
        return Permission(True)

    command = input.get("command")
    if not isinstance(command, str):
        return Permission(False, "a Bash call with no command in it")
    for forbidden in FORBIDDEN_SHELL:
        if forbidden in command:
            return Permission(
                False,
                f"`{forbidden}` can redirect output into the worktree. Read the file and reason "
                "about it; do not write one.",
            )
    for link in _CHAIN.split(command):
        permission = _permitted_command(shlex.split(link), suite)
        if not permission.allowed:
            return permission
    return Permission(True)


# ── what the Editor must return ──────────────────────────────────────────────────────────────


def parse_verdict(output: str) -> EditorVerdict | None:
    """The verdict, or nothing.

    Nothing is not a bug here — it is `infra-failed`, and it is the honest classification of an
    Editor that was asked a question and did not answer it. The adapter must never invent a verdict
    to fill the hole: a fabricated `inconclusive` reads, downstream, exactly like a considered one.
    """
    start = output.find(VERDICT_OPEN)
    if start == -1:
        return None
    end = output.find(VERDICT_CLOSE, start)
    if end == -1:
        raise VerdictParseError(f"{VERDICT_OPEN} with no {VERDICT_CLOSE}")

    body = output[start + len(VERDICT_OPEN) : end]
    try:
        raw = json.loads(body)
        verdict = Verdict(raw["verdict"])
        brief = raw.get("revised_brief")
        findings = raw.get("revised_findings")
        return EditorVerdict(
            verdict=verdict,
            revised_brief=Brief(body=brief) if brief else None,
            revised_findings=Findings(body=findings) if findings else None,
            rationale=raw["rationale"],
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise VerdictParseError(f"unreadable verdict: {body!r}") from exc


# ── how the Editor is bounded ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token consumption reported by a model call."""

    consumed_tokens: int


Turn = str | TokenUsage
"""What a session emits as it goes: something it said, or usage from a model call."""


@runtime_checkable
class EditorSession(Protocol):
    """A running Editor conversation. The SDK, or a CLI, or a stub — the bounding does not care.

    `kill()` must make `turns()` **end**, not raise: it is how the wall clock stops a session, and
    a kill that surfaced as a `CancelledError` three layers up would be reported as `infra-failed`
    for the wrong reason.
    """

    @property
    def returncode(self) -> int | None: ...

    def turns(self) -> AsyncGenerator[Turn, None]: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...

async def run_editor(
    session: EditorSession, budget: Budget
) -> tuple[SessionTelemetry, EditorVerdict | None]:
    """One Editor session, under the same wall-clock bound as an Implementer's.

    `commits` is zero and `diffstat` empty *by construction*, not by observation: the Editor was
    denied every tool that could have made them otherwise.
    """
    started = time.monotonic()
    said: list[str] = []
    consumed_tokens = 0

    async def pump() -> None:
        nonlocal consumed_tokens
        async for turn in session.turns():
            if isinstance(turn, TokenUsage):
                consumed_tokens = max(consumed_tokens, turn.consumed_tokens)
            else:
                said.append(turn)

    reading = asyncio.create_task(pump())
    bound = await run_bounded(_TurnStreamKillable(session, reading), budget)
    await reading
    bound = Bound(killed=bound.killed, consumed_tokens=consumed_tokens)

    output = "".join(said)
    telemetry = editor_telemetry(
        bound=bound,
        exit_code=session.returncode if session.returncode is not None else -1,
        output=output,
        wall_clock_s=time.monotonic() - started,
    )
    return telemetry, verdict_of(output)


def editor_telemetry(bound: Bound, exit_code: int, output: str, wall_clock_s: float) -> SessionTelemetry:
    """An Editor's telemetry, however it was run — an SDK conversation or a CLI subprocess.

    `commits` is zero and `diffstat` empty **by construction, not by observation**: the Editor was
    denied every tool that could have made them otherwise. Measuring them would suggest they might
    come back non-zero, and if they ever did, the honest response is not to report it — it is that
    the enforcement surface has failed and the Editor has become an Implementer.
    """
    return SessionTelemetry(
        exit_code=exit_code,
        killed=bound.killed,
        consumed_tokens=bound.consumed_tokens,
        wall_clock_s=wall_clock_s,
        commits=0,
        diffstat="",
        session_output=output,
        impasse_report=None,  # an Editor cannot declare an impasse. It adjudicates them.
    )


@dataclass(frozen=True, slots=True)
class _TurnStreamKillable:
    """Clock-bound the turn stream, because an in-process Editor may not have subprocess wait
    semantics. The session is finished when its turns are drained."""

    session: EditorSession
    reading: asyncio.Task[None]

    @property
    def returncode(self) -> int | None:
        return self.session.returncode

    def kill(self) -> None:
        self.session.kill()

    async def wait(self) -> int:
        await asyncio.shield(self.reading)
        return await self.session.wait()


def verdict_of(output: str) -> EditorVerdict | None:
    """A verdict the harness cannot read is not worth killing the run over.

    An unreadable *impasse* is fatal, because it would be misclassified as an undeclared impasse,
    silently dropping the model's claim. An unreadable *verdict* has no such problem: `None`
    classifies `infra-failed`, which pages a human and preserves the worktree —
    which is exactly where a garbled verdict belongs. So it is logged loudly and the run goes on,
    rather than taking twenty healthy sub-issues down with it.
    """
    try:
        return parse_verdict(output)
    except VerdictParseError:
        log.exception("the Editor returned a verdict the harness cannot read")
        return None
