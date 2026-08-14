"""Codex's JSONL `codex exec` turn-stream boundary."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from functools import partial
from pathlib import Path

from ralph.adapters.runtime.prompt import implementer_prompt
from ralph.adapters.runtime.session import STDOUT_LINE_LIMIT, Session
from ralph.adapters.runtime.turn_stream import (
    AutoCompaction,
    Turn,
    TurnStreamAsk,
    TurnStreamSession,
)
from ralph.harness import TokenConsumption
from ralph.issues import Findings, Spec
from ralph.ports import HARNESS_LINE, Worktree

MODEL = "gpt-5.5"
IMPLEMENTER_SANDBOX = "workspace-write"
EDITOR_SANDBOX = "read-only"
APPROVAL = "never"

THREAD_STARTED = "thread.started"
TURN_COMPLETED = "turn.completed"
CONTEXT_COMPACTED = "context_compacted"
CONTEXT_COMPACTION = "context_compaction"

TOTAL_TOKENS = "total_tokens"
INPUT_TOKENS = "input_tokens"
CACHED_INPUT_TOKENS = "cached_input_tokens"
OUTPUT_TOKENS = "output_tokens"


class CodexUsageError(ValueError):
    """A completed Codex turn whose token usage the harness cannot read."""


def codex_argv(
    spec: Spec,
    findings: Findings,
    worktree: Worktree,
    writable: Sequence[Path],
    *,
    sandbox: str = IMPLEMENTER_SANDBOX,
) -> Sequence[str]:
    """`--json` is not optional: it is how the session reports completed-turn usage."""
    return _codex_exec_argv(
        prompt=implementer_prompt(spec, findings),
        cwd=worktree.path,
        sandbox=sandbox,
        writable=writable,
    )


def _writable_argv(writable: Sequence[Path]) -> list[str]:
    """`--cd` makes the worktree writable and stops there, which is one directory short of a
    commit: `git add` writes the index, and `git commit` writes objects and moves a ref, none of
    which live in the checkout. Without this the kernel refuses those writes and git reports
    `Read-only file system` — a session can then resolve everything correctly and land nothing.

    Empty for the Editor, which is denied writes on purpose.
    """
    return [arg for dir in writable for arg in ("--add-dir", str(dir))]


def _codex_exec_argv(*, prompt: str, cwd: Path, sandbox: str, writable: Sequence[Path]) -> list[str]:
    """Codex global options precede `exec`; `exec` options follow it."""
    return [
        "codex",
        "--ask-for-approval", APPROVAL,
        "exec",
        "--json",
        "--model", MODEL,
        "--sandbox", sandbox,
        "--cd", str(cwd),
        *_writable_argv(writable),
        prompt,
    ]


def _codex_resume_argv(
    *, prompt: str, cwd: Path, sandbox: str, writable: Sequence[Path], resumable_identifier: str
) -> list[str]:
    """`resume` is an `exec` subcommand, so `exec` options come before it."""
    return [
        "codex",
        "--ask-for-approval", APPROVAL,
        "exec",
        "--json",
        "--model", MODEL,
        "--sandbox", sandbox,
        "--cd", str(cwd),
        *_writable_argv(writable),
        "resume",
        resumable_identifier,
        prompt,
    ]


def _obj(value: object) -> dict[str, object]:
    """A JSON object, or an empty one. Every navigation below goes through this, so a missing
    branch reads as absent rather than as a `TypeError` three lines later."""
    return value if isinstance(value, dict) else {}


def _loads(line: str) -> dict[str, object]:
    try:
        return _obj(json.loads(line))
    except json.JSONDecodeError:
        return {}  # Codex prints human banner lines alongside its JSON stream


def thread_id(line: str) -> str | None:
    """The id Codex announces on `thread.started`, or nothing."""
    event = _loads(line)
    if event.get("type") != THREAD_STARTED:
        return None
    id = event.get("thread_id")
    return id if isinstance(id, str) else None


def _event_type(event: Mapping[str, object]) -> str:
    type = event.get("type")
    return type if isinstance(type, str) else ""


def _data(event: Mapping[str, object]) -> dict[str, object]:
    for key in ("data", "payload", "message", "event"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _first_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str):
            return value
    return None


def _text_of(event: Mapping[str, object]) -> str | None:
    """Text from the JSONL event shapes Codex has exposed across releases."""
    data = _data(event)
    direct = _first_str(
        event.get("delta"),
        event.get("text"),
        event.get("content"),
        event.get("message"),
        data.get("delta"),
        data.get("text"),
        data.get("content"),
        data.get("message"),
        data.get("delta_content"),
    )
    if direct is not None:
        return direct

    content = data.get("content")
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = _first_str(item.get("text"), item.get("content"))
                if text is not None:
                    chunks.append(text)
        if chunks:
            return "".join(chunks)
    return None


def _nested_usage(event: Mapping[str, object]) -> dict[str, object]:
    direct = event.get("usage")
    if isinstance(direct, dict):
        return direct
    for value in (event.get("data"), event.get("payload")):
        if isinstance(value, dict):
            usage = value.get("usage")
            if isinstance(usage, dict):
                return usage
    return {}


def _reported_consumption(event: Mapping[str, object]) -> TokenConsumption | None:
    """Codex's components, translated into the harness's three buckets — or a bare total.

    Components first: releases since codex-cli 0.143.0 drop `total_tokens` from `turn.completed` and
    send the components instead, so the total is the legacy path and reading it first would discard
    a breakdown that was right there.

    Codex counts cached tokens *inside* `input_tokens`, so the fresh half is the difference. It has
    no cache-write signal at all; those tokens are billed in `input_tokens` and stay there, which is
    what the harness's `input` bucket means. `reasoning_output_tokens` is likewise a breakdown *of*
    output, not an addend — adding it would double-count.
    """
    usage = _nested_usage(event)
    input_tokens = _optional_int(usage, INPUT_TOKENS)
    output_tokens = _optional_int(usage, OUTPUT_TOKENS)
    if input_tokens is not None or output_tokens is not None:
        cache_read = _optional_int(usage, CACHED_INPUT_TOKENS) or 0
        return TokenConsumption.split(
            input=max((input_tokens or 0) - cache_read, 0),
            cache_read=cache_read,
            output=output_tokens or 0,
        )
    total = _optional_int(usage, TOTAL_TOKENS)
    return None if total is None else TokenConsumption.total_only(total)


def _completed_turn_consumption(event: Mapping[str, object], line: str) -> TokenConsumption:
    """A `turn.completed` carrying no readable count is a schema break, not a zero."""
    consumption = _reported_consumption(event)
    if consumption is None:
        raise CodexUsageError(f"a turn.completed event with no token counts in it: {line!r}")
    return consumption


async def end_of_turn_consumption(session: Session, _worktree: Worktree) -> TokenConsumption | None:
    """What Codex reports when the `exec` turn completes, or none if it never completed."""
    found: TokenConsumption | None = None
    for line in session.output.splitlines():
        event = _loads(line)
        if _event_type(event) != TURN_COMPLETED:
            continue
        found = _completed_turn_consumption(event, line)
    return found


class CodexJsonSession(TurnStreamSession):
    """The typed wrapper around `codex exec --json`.

    Cancellation is process cancellation: `kill()` kills the child and the JSONL stream drains to
    EOF. Read-only enforcement for the Editor is not a callback here; it is the sandbox mode chosen
    by `codex_editor`.
    """

    def __init__(
        self,
        ask: TurnStreamAsk,
        *,
        sandbox: str,
        writable: Sequence[Path] = (),
        build_argv: Callable[[TurnStreamAsk, str], Sequence[str]] | None = None,
    ) -> None:
        self._ask = ask
        self._sandbox = sandbox
        self._build_argv = build_argv or partial(_session_argv, writable=writable)
        self._turns: asyncio.Queue[Turn | None] = asyncio.Queue()
        self._transcript: list[str] = []
        self._auto_compactions: list[AutoCompaction] = []
        self._resumable_identifier = ask.resumable_identifier
        self._proc: asyncio.subprocess.Process | None = None
        self._code: int | None = None
        self._stream_closed = False
        self._running = asyncio.create_task(self._run())

    @property
    def returncode(self) -> int | None:
        if self._proc is not None and self._proc.returncode is not None:
            return self._proc.returncode
        return self._code

    @property
    def auto_compactions(self) -> tuple[AutoCompaction, ...]:
        return tuple(self._auto_compactions)

    @property
    def resumable_identifier(self) -> str | None:
        return self._resumable_identifier

    @property
    def transcript(self) -> str:
        return "".join(self._transcript)

    async def turns(self) -> AsyncGenerator[Turn, None]:
        while (turn := await self._turns.get()) is not None:
            yield turn

    def kill(self) -> None:
        if self._running.done():
            return
        self._code = -9
        if self._proc is None:
            self._running.cancel()
            self._close_stream()
            return
        if self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return

    async def wait(self) -> int:
        await self._running
        return self.returncode if self.returncode is not None else -1

    async def _run(self) -> None:
        try:
            argv = self._build_argv(self._ask, self._sandbox)
            # The launch, at the top of the session's own record. The argv carries the model, the
            # sandbox, every writable directory and the whole prompt, so a prompt this actor was
            # never given is legible from the artifact rather than only from reading `prompt.py`.
            self._transcript.append(f"{HARNESS_LINE}launched: {shlex.join(argv)}\n")
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._ask.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                limit=STDOUT_LINE_LIMIT,
            )
            if self._proc.stdout is None:  # pragma: no cover — PIPE was asked for above
                raise RuntimeError("the Codex session has no stdout to read")
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace")
                self._transcript.append(line)  # recorded first. `_observe` only ever reads it.
                self._observe(line)
            code = await self._proc.wait()
            if self._code is None:
                self._code = code
        except asyncio.CancelledError:
            self._code = -9
        finally:
            self._close_stream()

    def _observe(self, line: str) -> None:
        event = _loads(line)
        if not event:
            if line:
                self._turns.put_nowait(line)
            return

        kind = _event_type(event)
        if kind == THREAD_STARTED:
            id = event.get("thread_id")
            if isinstance(id, str):
                self._resumable_identifier = id

        if kind == TURN_COMPLETED:
            self._turns.put_nowait(_completed_turn_consumption(event, line))

        if _is_compaction(kind):
            self._auto_compactions.append(_compaction_of(kind, event))

        text = _text_of(event)
        if text:
            self._turns.put_nowait(text)

    def _close_stream(self) -> None:
        if not self._stream_closed:
            self._stream_closed = True
            self._turns.put_nowait(None)


def _session_argv(ask: TurnStreamAsk, sandbox: str, *, writable: Sequence[Path]) -> Sequence[str]:
    if ask.resumable_identifier is not None:
        return _codex_resume_argv(
            prompt=ask.prompt,
            cwd=ask.cwd,
            sandbox=sandbox,
            writable=writable,
            resumable_identifier=ask.resumable_identifier,
        )
    return _codex_exec_argv(prompt=ask.prompt, cwd=ask.cwd, sandbox=sandbox, writable=writable)


def _is_compaction(kind: str) -> bool:
    compacted = kind.replace(".", "_")
    return "compact" in compacted or kind in {CONTEXT_COMPACTED, CONTEXT_COMPACTION}


def _optional_int(data: Mapping[str, object], name: str) -> int | None:
    value = data.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_bool(data: Mapping[str, object], name: str) -> bool | None:
    value = data.get(name)
    return value if isinstance(value, bool) else None


def _compaction_of(kind: str, event: Mapping[str, object]) -> AutoCompaction:
    data = _data(event) or dict(event)
    started = "start" in kind or "request" in kind or "trigger" in kind
    return AutoCompaction(
        event="started" if started else "compacted",
        success=_optional_bool(data, "success"),
        conversation_tokens=_optional_int(data, "conversation_tokens"),
        pre_compaction_tokens=_optional_int(data, "pre_compaction_tokens"),
        post_compaction_tokens=_optional_int(data, "post_compaction_tokens"),
        tokens_removed=_optional_int(data, "tokens_removed"),
    )


def codex_sdk_session(ask: TurnStreamAsk, git_metadata: Path) -> CodexJsonSession:
    """For the two actors that commit. `git_metadata` is not optional for either of them: an
    Implementer that cannot commit is an `impasse`, and an Integrator that cannot commit is a
    human being paged."""
    return CodexJsonSession(ask=ask, sandbox=IMPLEMENTER_SANDBOX, writable=(git_metadata,))


def codex_read_only_session(ask: TurnStreamAsk) -> CodexJsonSession:
    return CodexJsonSession(ask=ask, sandbox=EDITOR_SANDBOX)
