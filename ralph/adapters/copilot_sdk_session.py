"""Copilot SDK session boundary.

The SDK stays behind this seam. Ralph sees a stream of text or usage observations, a small record of
auto-compaction events, and a `kill()` that aborts the active SDK turn and ends the stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from ralph.adapters.editor import TurnStreamAsk, TurnStreamSession, Permit, TokenUsage, Turn

if TYPE_CHECKING:
    from copilot.generated.session_events import PermissionRequest as SdkPermissionRequest
    from copilot.session import PermissionRequestResult as SdkPermissionResult
else:
    SdkPermissionRequest = object
    SdkPermissionResult = object

ASSISTANT_MESSAGE_DELTA = "assistant.message_delta"
ASSISTANT_MESSAGE = "assistant.message"
ASSISTANT_USAGE = "assistant.usage"
ASSISTANT_TURN_END = "assistant.turn_end"
SESSION_COMPACTION_START = "session.compaction_start"
SESSION_COMPACTION_FINISHED = "session.compaction_complete"
SESSION_IDLE = "session.idle"
ASSISTANT_IDLE = "assistant.idle"

TOTAL_TOKENS = "total_tokens"
INPUT_TOKENS = "input_tokens"
CACHE_READ_TOKENS = "cache_read_tokens"
CACHE_WRITE_TOKENS = "cache_write_tokens"
OUTPUT_TOKENS = "output_tokens"
REASONING_TOKENS = "reasoning_tokens"

DELTA_CONTENT = "delta_content"
CONTENT = "content"
CONVERSATION_TOKENS = "conversation_tokens"
PRE_COMPACTION_TOKENS = "pre_compaction_tokens"
POST_COMPACTION_TOKENS = "post_compaction_tokens"
TOKENS_REMOVED = "tokens_removed"
SUCCESS = "success"


class CopilotSdkUsageError(RuntimeError):
    """A Copilot SDK usage event whose token total Ralph cannot read."""


@dataclass(frozen=True, slots=True)
class AutoCompaction:
    """An SDK auto-compaction event, captured before persistence exists."""

    event: Literal["started", "compacted"]
    success: bool | None = None
    conversation_tokens: int | None = None
    pre_compaction_tokens: int | None = None
    post_compaction_tokens: int | None = None
    tokens_removed: int | None = None


class SdkEvent(Protocol):
    @property
    def type(self) -> object: ...

    @property
    def data(self) -> object: ...


class RunningCopilotSession(Protocol):
    def on(self, handler: Callable[[SdkEvent], None]) -> Callable[[], None]: ...

    async def send(self, prompt: str) -> str: ...

    async def abort(self) -> None: ...

    async def disconnect(self) -> None: ...


SdkPermissionHandler = Callable[[SdkPermissionRequest, dict[str, str]], SdkPermissionResult]
CreateSession = Callable[
    [TurnStreamAsk, Callable[[SdkEvent], None], SdkPermissionHandler | None],
    Awaitable[RunningCopilotSession],
]


class CopilotSdkSession(TurnStreamSession):
    def __init__(self, ask: TurnStreamAsk, *, create_session: CreateSession) -> None:
        self._ask = ask
        self._create_session = create_session
        self._turns: asyncio.Queue[Turn | None] = asyncio.Queue()
        self._idle = asyncio.Event()
        self._auto_compactions: list[AutoCompaction] = []
        self._code: int | None = None
        self._running_session: RunningCopilotSession | None = None
        self._aborting: asyncio.Task[None] | None = None
        self._saw_message_delta = False
        self._consumed_tokens = 0
        self._observed_usage = False
        self._emitted_usage = False
        self._stream_closed = False
        self._running = asyncio.create_task(self._converse())

    @property
    def returncode(self) -> int | None:
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
        if self._running_session is not None:
            self._aborting = asyncio.create_task(self._running_session.abort())
        self._close_stream()
        self._running.cancel()

    async def wait(self) -> int:
        await self._running
        if self._aborting is not None:
            await asyncio.gather(self._aborting, return_exceptions=True)
        return self._code if self._code is not None else -1

    async def _converse(self) -> None:
        try:
            self._running_session = await self._create_session(
                self._ask, self._observe, _permission_handler(self._ask.permit)
            )
            await self._running_session.send(self._ask.prompt)
            await self._idle.wait()
            if self._code is None:
                self._code = 0
        except asyncio.CancelledError:
            if self._code is None:
                self._code = -9
        finally:
            if self._running_session is not None:
                await self._running_session.disconnect()
            self._close_stream()

    def _observe(self, event: SdkEvent) -> None:
        kind = _event_type(event)
        data = event.data

        if kind == ASSISTANT_MESSAGE_DELTA:
            self._saw_message_delta = True
            self._put_text(_str_attr(data, DELTA_CONTENT))
        elif kind == ASSISTANT_MESSAGE and not self._saw_message_delta:
            self._put_text(_str_attr(data, CONTENT))
        elif kind == ASSISTANT_USAGE:
            self._observed_usage = True
            self._consumed_tokens += _usage_tokens(data)
        elif kind == SESSION_COMPACTION_START:
            self._auto_compactions.append(
                AutoCompaction(
                    event="started",
                    conversation_tokens=_optional_int_attr(data, CONVERSATION_TOKENS),
                )
            )
        elif kind == SESSION_COMPACTION_FINISHED:
            self._auto_compactions.append(
                AutoCompaction(
                    event="compacted",
                    success=_optional_bool_attr(data, SUCCESS),
                    conversation_tokens=_optional_int_attr(data, CONVERSATION_TOKENS),
                    pre_compaction_tokens=_optional_int_attr(data, PRE_COMPACTION_TOKENS),
                    post_compaction_tokens=_optional_int_attr(data, POST_COMPACTION_TOKENS),
                    tokens_removed=_optional_int_attr(data, TOKENS_REMOVED),
                )
            )
        elif kind == ASSISTANT_TURN_END:
            self._emit_usage()
        elif kind in {SESSION_IDLE, ASSISTANT_IDLE}:
            self._emit_usage()
            self._idle.set()

    def _put_text(self, text: str | None) -> None:
        if text:
            self._turns.put_nowait(text)

    def _close_stream(self) -> None:
        if not self._stream_closed:
            self._stream_closed = True
            self._turns.put_nowait(None)

    def _emit_usage(self) -> None:
        if self._observed_usage and not self._emitted_usage:
            self._turns.put_nowait(TokenUsage(consumed_tokens=self._consumed_tokens))
            self._emitted_usage = True


class _CopilotClient(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass(slots=True)
class _RuntimeSession:
    client: _CopilotClient
    session: RunningCopilotSession
    unsubscribe: Callable[[], None]

    def on(self, handler: Callable[[SdkEvent], None]) -> Callable[[], None]:
        return self.session.on(handler)

    async def send(self, prompt: str) -> str:
        return await self.session.send(prompt)

    async def abort(self) -> None:
        await self.session.abort()

    async def disconnect(self) -> None:
        self.unsubscribe()
        await self.session.disconnect()
        await self.client.stop()


async def _open_real_session(
    ask: TurnStreamAsk,
    observe: Callable[[SdkEvent], None],
    on_permission_request: SdkPermissionHandler | None,
) -> RunningCopilotSession:
    from copilot import CopilotClient

    client = CopilotClient(working_directory=str(ask.cwd))
    await client.start()
    try:
        session = cast(
            RunningCopilotSession,
            await client.create_session(
                streaming=True,
                on_permission_request=on_permission_request,
            ),
        )
    except Exception:
        await client.stop()
        raise
    return _RuntimeSession(
        client=cast(_CopilotClient, client), session=session, unsubscribe=session.on(observe)
    )


def copilot_sdk_session(ask: TurnStreamAsk) -> CopilotSdkSession:
    return CopilotSdkSession(ask, create_session=_open_real_session)


def _permission_handler(permit: Permit | None) -> SdkPermissionHandler | None:
    if permit is None:
        return None

    def on_permission_request(
        request: SdkPermissionRequest, metadata: dict[str, str]
    ) -> SdkPermissionResult:
        tool, input = _permission_tool_input(request, metadata)
        permission = permit(tool, input)
        if permission.allowed:
            return _approve_once()
        return _reject_permission(permission.reason)

    return on_permission_request


def _permission_tool_input(
    request: object, metadata: Mapping[str, str]
) -> tuple[str, dict[str, object]]:
    kind = _str_attr(request, "kind") or ""
    if kind == "shell":
        return "Bash", {"command": _str_attr(request, "full_command_text") or ""}
    if kind == "read":
        return "Read", {"file_path": _str_attr(request, "path") or ""}
    if kind == "write":
        return "Write", {"file_path": _str_attr(request, "file_name") or ""}
    if kind == "custom-tool":
        return _str_attr(request, "tool_name") or kind, _mapping_attr(request, "args")
    if kind == "mcp":
        return _str_attr(request, "tool_name") or kind, _mapping_attr(request, "args")
    return kind or request.__class__.__name__, dict(metadata)


def _approve_once() -> SdkPermissionResult:
    from copilot.generated.rpc import PermissionDecisionApproveOnce

    return PermissionDecisionApproveOnce()


def _reject_permission(reason: str) -> SdkPermissionResult:
    from copilot.generated.rpc import PermissionDecisionReject

    return PermissionDecisionReject(feedback=reason)


def _event_type(event: SdkEvent) -> str:
    raw = event.type
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else ""


def _usage_tokens(data: object) -> int:
    total = _optional_int_attr(data, TOTAL_TOKENS)
    if total is not None:
        return total
    parts = (
        _optional_int_attr(data, INPUT_TOKENS),
        _optional_int_attr(data, CACHE_READ_TOKENS),
        _optional_int_attr(data, CACHE_WRITE_TOKENS),
        _optional_int_attr(data, OUTPUT_TOKENS),
        _optional_int_attr(data, REASONING_TOKENS),
    )
    if all(tokens is None for tokens in parts):
        raise CopilotSdkUsageError(f"a usage event with no token counts in it: {data!r}")
    return sum(tokens for tokens in parts if tokens is not None)


def _str_attr(data: object, name: str) -> str | None:
    value = getattr(data, name, None)
    return value if isinstance(value, str) else None


def _mapping_attr(data: object, name: str) -> dict[str, object]:
    value = getattr(data, name, None)
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_int_attr(data: object, name: str) -> int | None:
    value = getattr(data, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_bool_attr(data: object, name: str) -> bool | None:
    value = getattr(data, name, None)
    return value if isinstance(value, bool) else None
