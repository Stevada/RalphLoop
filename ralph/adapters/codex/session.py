"""Codex's JSONL `codex exec` turn-stream boundary."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from pathlib import Path

from ralph.adapters.prompt import implementer_prompt
from ralph.adapters.session import Session
from ralph.adapters.turn_stream import (
    AutoCompaction,
    TokenUsage,
    Turn,
    TurnStreamAsk,
    TurnStreamSession,
)
from ralph.issues import Brief, Findings
from ralph.ports import Worktree

MODEL = "gpt-5.3-codex"
IMPLEMENTER_SANDBOX = "workspace-write"
EDITOR_SANDBOX = "read-only"
APPROVAL = "never"

THREAD_STARTED = "thread.started"
TURN_COMPLETED = "turn.completed"
CONTEXT_COMPACTED = "context_compacted"
CONTEXT_COMPACTION = "context_compaction"


class CodexUsageError(ValueError):
    """A completed Codex turn whose token usage the harness cannot read."""


def codex_argv(
    brief: Brief,
    findings: Findings,
    worktree: Worktree,
    *,
    sandbox: str = IMPLEMENTER_SANDBOX,
) -> Sequence[str]:
    """`--json` is not optional: it is how the session reports completed-turn usage."""
    return _codex_exec_argv(
        prompt=implementer_prompt(brief, findings),
        cwd=worktree.path,
        sandbox=sandbox,
    )


def _codex_exec_argv(*, prompt: str, cwd: Path, sandbox: str) -> list[str]:
    """Codex global options precede `exec`; `exec` options follow it."""
    return [
        "codex",
        "--ask-for-approval", APPROVAL,
        "exec",
        "--json",
        "--model", MODEL,
        "--sandbox", sandbox,
        "--cd", str(cwd),
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


def _usage_total(event: Mapping[str, object]) -> int | None:
    total = _nested_usage(event).get("total_tokens")
    return total if isinstance(total, int) and not isinstance(total, bool) else None


async def end_of_turn_consumed_tokens(session: Session, _worktree: Worktree) -> int | None:
    """The total Codex reports when the `exec` turn completes, or none if it never completed."""
    found: int | None = None
    for line in session.output.splitlines():
        event = _loads(line)
        if _event_type(event) != TURN_COMPLETED:
            continue
        total = _usage_total(event)
        if total is None:
            raise CodexUsageError(f"a turn.completed event with no total_tokens in it: {line!r}")
        found = total
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
        build_argv: Callable[[TurnStreamAsk, str], Sequence[str]] | None = None,
    ) -> None:
        self._ask = ask
        self._sandbox = sandbox
        self._build_argv = build_argv or _session_argv
        self._turns: asyncio.Queue[Turn | None] = asyncio.Queue()
        self._auto_compactions: list[AutoCompaction] = []
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
            self._proc = await asyncio.create_subprocess_exec(
                *self._build_argv(self._ask, self._sandbox),
                cwd=self._ask.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            if self._proc.stdout is None:  # pragma: no cover — PIPE was asked for above
                raise RuntimeError("the Codex session has no stdout to read")
            async for raw in self._proc.stdout:
                self._observe(raw.decode(errors="replace"))
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
        if kind == TURN_COMPLETED:
            total = _usage_total(event)
            if total is None:
                raise CodexUsageError(f"a turn.completed event with no total_tokens in it: {line!r}")
            self._turns.put_nowait(TokenUsage(consumed_tokens=total))

        if _is_compaction(kind):
            self._auto_compactions.append(_compaction_of(kind, event))

        text = _text_of(event)
        if text:
            self._turns.put_nowait(text)

    def _close_stream(self) -> None:
        if not self._stream_closed:
            self._stream_closed = True
            self._turns.put_nowait(None)


def _session_argv(ask: TurnStreamAsk, sandbox: str) -> Sequence[str]:
    return _codex_exec_argv(prompt=ask.prompt, cwd=ask.cwd, sandbox=sandbox)


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


def codex_sdk_session(ask: TurnStreamAsk) -> CodexJsonSession:
    return CodexJsonSession(ask=ask, sandbox=IMPLEMENTER_SANDBOX)


def codex_read_only_session(ask: TurnStreamAsk) -> CodexJsonSession:
    return CodexJsonSession(ask=ask, sandbox=EDITOR_SANDBOX)
