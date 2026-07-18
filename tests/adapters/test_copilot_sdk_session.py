"""The Copilot SDK session seam, driven by a stub SDK session.

No test here starts the real SDK runtime or calls a model. The SDK event names and fields come from
the public Python SDK surface; the behavior under test is Ralph's translation at the boundary.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from ralph.adapters.copilot_sdk_session import (
    ASSISTANT_MESSAGE,
    ASSISTANT_MESSAGE_DELTA,
    ASSISTANT_TURN_END,
    ASSISTANT_USAGE,
    AutoCompaction,
    CopilotSdkUsageError,
    CopilotSdkSession,
    RunningCopilotSession,
    SESSION_COMPACTION_FINISHED,
    SESSION_IDLE,
    SdkEvent,
    SdkPermissionHandler,
    copilot_sdk_session,
)
from ralph.adapters.editor import TurnStreamAsk, TokenUsage, Turn, read_only

if TYPE_CHECKING:
    from copilot.generated.session_events import PermissionRequest
else:
    PermissionRequest = object

SUITE: Sequence[str] = ("uv", "run", "pytest", "-q")


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    data: object


@dataclass(frozen=True, slots=True)
class Text:
    content: str


@dataclass(frozen=True, slots=True)
class Delta:
    delta_content: str


@dataclass(frozen=True, slots=True)
class Usage:
    total_tokens: int


@dataclass(frozen=True, slots=True)
class SplitUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CompactionFinished:
    success: bool
    pre_compaction_tokens: int
    post_compaction_tokens: int
    tokens_removed: int


@dataclass(frozen=True, slots=True)
class ShellRequest:
    full_command_text: str
    kind: str = "shell"


@dataclass(frozen=True, slots=True)
class WriteRequest:
    file_name: str
    kind: str = "write"


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str = ""


class StubCopilotSession:
    def __init__(
        self,
        events: Sequence[SdkEvent],
        *,
        permission_requests: Sequence[object] = (),
        pause: float = 0.0,
    ) -> None:
        self._events = events
        self._permission_requests = permission_requests
        self._pause = pause
        self._handler: Callable[[SdkEvent], None] | None = None
        self._permission_handler: SdkPermissionHandler | None = None
        self.sent: list[str] = []
        self.decisions: list[object] = []
        self.executed_permission_requests: list[object] = []
        self.aborted = False
        self.disconnected = False

    def on(self, handler: Callable[[SdkEvent], None]) -> Callable[[], None]:
        self._handler = handler

        def unsubscribe() -> None:
            self._handler = None

        return unsubscribe

    def on_permission_request(
        self, handler: SdkPermissionHandler | None
    ) -> None:
        self._permission_handler = handler

    async def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        for request in self._permission_requests:
            if self._permission_handler is None:
                self.executed_permission_requests.append(request)
                continue
            decision = self._permission_handler(cast(PermissionRequest, request), {})
            self.decisions.append(decision)
            if getattr(decision, "allowed", False):
                self.executed_permission_requests.append(request)
        for event in self._events:
            if self.aborted:
                return "message-1"
            if self._handler is not None:
                self._handler(event)
            if self._pause:
                await asyncio.sleep(self._pause)
        return "message-1"

    async def abort(self) -> None:
        self.aborted = True

    async def disconnect(self) -> None:
        self.disconnected = True


def open_session(
    stub: StubCopilotSession,
    *,
    ask: TurnStreamAsk = TurnStreamAsk(prompt="build it", cwd=Path("/w/01")),
) -> CopilotSdkSession:
    async def create(
        _ask: TurnStreamAsk,
        observe: Callable[[SdkEvent], None],
        on_permission_request: SdkPermissionHandler | None,
    ) -> RunningCopilotSession:
        stub.on(observe)
        stub.on_permission_request(on_permission_request)
        return stub

    return CopilotSdkSession(
        ask,
        create_session=create,
    )


async def collect(session: CopilotSdkSession) -> list[Turn]:
    return [turn async for turn in session.turns()]


async def test_turns_streams_text_and_usage_observations() -> None:
    stub = StubCopilotSession(
        [
            Event(ASSISTANT_MESSAGE, Text("I will inspect the repo.")),
            Event(ASSISTANT_USAGE, Usage(total_tokens=9_999)),
            Event(SESSION_IDLE, object()),
        ]
    )
    session = open_session(stub)

    turns = await collect(session)

    assert stub.sent == ["build it"]
    assert turns == ["I will inspect the repo.", TokenUsage(consumed_tokens=9_999)]
    assert session.returncode == 0
    assert stub.disconnected


async def test_consumption_is_one_end_of_turn_usage_observation() -> None:
    stub = StubCopilotSession(
        [
            Event(
                ASSISTANT_USAGE,
                SplitUsage(
                    input_tokens=100,
                    cache_read_tokens=7,
                    cache_write_tokens=11,
                    output_tokens=50,
                    reasoning_tokens=3,
                ),
            ),
            Event(ASSISTANT_USAGE, Usage(total_tokens=9)),
            Event(ASSISTANT_TURN_END, object()),
            Event(SESSION_IDLE, object()),
        ]
    )

    assert await collect(open_session(stub)) == [TokenUsage(consumed_tokens=180)]


async def test_a_usage_event_without_token_counts_is_loud() -> None:
    session = open_session(
        StubCopilotSession(
            [
                Event(ASSISTANT_USAGE, object()),
                Event(SESSION_IDLE, object()),
            ]
        )
    )

    assert await collect(session) == []
    with pytest.raises(CopilotSdkUsageError):
        await session.wait()


async def test_compaction_events_are_captured_for_later_persistence() -> None:
    stub = StubCopilotSession(
        [
            Event(
                SESSION_COMPACTION_FINISHED,
                CompactionFinished(
                    success=True,
                    pre_compaction_tokens=170_000,
                    post_compaction_tokens=43_000,
                    tokens_removed=127_000,
                ),
            ),
            Event(SESSION_IDLE, object()),
        ]
    )
    session = open_session(stub)

    await collect(session)

    assert session.auto_compactions == (
        AutoCompaction(
            event="compacted",
            success=True,
            pre_compaction_tokens=170_000,
            post_compaction_tokens=43_000,
            tokens_removed=127_000,
        ),
    )


async def test_permission_handler_denies_mutating_requests_pre_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ralph.adapters.copilot_sdk_session._approve_once",
        lambda: Decision(allowed=True),
    )
    monkeypatch.setattr(
        "ralph.adapters.copilot_sdk_session._reject_permission",
        lambda reason: Decision(allowed=False, reason=reason),
    )
    stub = StubCopilotSession(
        [Event(SESSION_IDLE, object())],
        permission_requests=[
            WriteRequest("calculator.py"),
            ShellRequest("git commit -m fixed"),
            ShellRequest("git cherry-pick abc123"),
        ],
    )
    session = open_session(
        stub,
        ask=TurnStreamAsk(
            prompt="adjudicate",
            cwd=Path("/w/01"),
            permit=lambda tool, input: read_only(tool, input, SUITE),
        ),
    )

    await collect(session)

    assert stub.executed_permission_requests == []
    assert stub.decisions == [
        Decision(allowed=False, reason="`Write` can modify the worktree. The Editor is read-only."),
        Decision(allowed=False, reason="`git commit` may write to the repository. You are read-only."),
        Decision(
            allowed=False,
            reason="`git cherry-pick` may write to the repository. You are read-only.",
        ),
    ]


async def test_kill_aborts_the_sdk_session_and_ends_the_stream() -> None:
    stub = StubCopilotSession(
        [Event(ASSISTANT_MESSAGE_DELTA, Delta("still working"))],
        pause=60.0,
    )
    session = open_session(stub)
    seen: list[Turn] = []

    async def read() -> None:
        async for turn in session.turns():
            seen.append(turn)

    reading = asyncio.create_task(read())
    await asyncio.sleep(0)
    session.kill()
    await reading
    await session.wait()

    assert seen == ["still working"]
    assert stub.aborted
    assert session.returncode == -9


REAL = pytest.mark.skipif(
    os.environ.get("RALPH_REAL_COPILOT_SDK") != "1",
    reason="set RALPH_REAL_COPILOT_SDK=1 to spend real Copilot SDK tokens",
)


@REAL
async def test_a_real_copilot_sdk_session_can_be_killed(tmp_path: Path) -> None:
    session = copilot_sdk_session(
        TurnStreamAsk(
            prompt="Count upward one number per line until you are stopped.",
            cwd=tmp_path,
        )
    )
    reading = asyncio.create_task(collect(session))

    await asyncio.sleep(5.0)
    session.kill()
    await asyncio.wait_for(reading, timeout=15.0)

    assert await session.wait() == -9
