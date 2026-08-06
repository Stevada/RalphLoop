"""Claude Code SDK session boundary.

This is the strongest read-only guarantee available to any Editor in the system, and it is why this
adapter exists alongside the coarser Copilot one: the SDK's `can_use_tool` callback lets the
*harness* adjudicate every tool call before it happens. A mutating call is **denied**, not
discouraged.

The SDK itself is behind one seam — `OpenSession` — and everything the harness owns sits on this
side of it: the permit, the prompt, the verdict, both bounds. The tests drive a stub through that
seam, so **no test in this file calls the Anthropic API**. What is left on the far side is a few
lines of message translation, and they are the only lines here that a real session exercises.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from ralph.adapters.runtime.editor import READ_ONLY_TOOLS
from ralph.adapters.runtime.turn_stream import (
    AutoCompaction,
    Turn,
    TurnStreamAsk,
    TurnStreamSession,
)
from ralph.harness import NOTHING, TokenConsumption

MODEL = "claude-opus-5"
"""The Editor is the expensive one on purpose. It runs at most three times per sub-issue and it is
the only actor whose judgment the harness cannot check against a suite."""


class _SdkSession:
    """The SDK's stream, reshaped into the two things the harness wants from it: what the model
    said, and what each call cost.
    """

    def __init__(self, ask: TurnStreamAsk) -> None:
        self._ask = ask
        self._turns: asyncio.Queue[Turn | None] = asyncio.Queue()
        self._code: int | None = None
        self._running = asyncio.create_task(self._converse())

    async def _converse(self) -> None:
        # Nothing above the `try`. This coroutine runs as a bare task nobody awaits, so an exception
        # raised before it is a *silent* one, and the sentinel in `finally` is what tells the reader
        # the stream ended — without it, the reader waits on an empty queue for the life of the run.
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions,
                PermissionResultAllow,
                PermissionResultDeny,
                query,
            )

            async def can_use_tool(tool: str, input: dict[str, object], context: object) -> object:
                """**The enforcement surface.** The harness adjudicates the call before it
                happens."""
                assert self._ask.permit is not None
                permission = self._ask.permit(tool, input)
                if permission.allowed:
                    return PermissionResultAllow()
                return PermissionResultDeny(message=permission.reason)

            options = ClaudeAgentOptions(
                model=MODEL,
                allowed_tools=sorted(READ_ONLY_TOOLS),
                can_use_tool=can_use_tool,
                cwd=str(self._ask.cwd),
            )
            # The SDK bills per message, and a `Turn` is a running total — so the accumulating
            # happens here, where it is known that these are increments.
            consumed = NOTHING
            async for message in query(prompt=self._ask.prompt, options=options):
                for turn in _turns_of(message):
                    if isinstance(turn, TokenConsumption):
                        consumed += turn
                        self._turns.put_nowait(consumed)
                    else:
                        self._turns.put_nowait(turn)
            self._code = 0
        except asyncio.CancelledError:
            self._code = -9
        except Exception as failure:
            # Into the transcript, not just into a return code: an Editor session that ends with no
            # verdict is `infra-failed`, and the transcript is where the human reads *why*.
            self._code = 1
            self._turns.put_nowait(f"\nthe Claude SDK session failed: {failure!r}\n")
        finally:
            self._turns.put_nowait(None)

    @property
    def returncode(self) -> int | None:
        return self._code

    @property
    def auto_compactions(self) -> tuple[AutoCompaction, ...]:
        return ()

    @property
    def resumable_identifier(self) -> str | None:
        return None

    async def turns(self) -> AsyncGenerator[Turn, None]:
        while (turn := await self._turns.get()) is not None:
            yield turn

    def kill(self) -> None:
        # The sentinel unconditionally, cancelled or not: `TurnStreamSession` requires that `kill()`
        # *end* `turns()`, and cancelling a task that has already finished does nothing at all. A
        # kill that cannot end the stream is how a dead conversation outlives the wall clock.
        self._running.cancel()
        self._turns.put_nowait(None)

    async def wait(self) -> int:
        await asyncio.gather(self._running, return_exceptions=True)
        return self._code if self._code is not None else -1


def _turns_of(message: object) -> list[Turn]:
    """The SDK's untyped message, translated into our values at the boundary and never carried
    inward as a `dict[str, Any]`.

    Text and usage arrive interleaved in one iterator, which is why `Turn` is a union rather than
    two streams: splitting them is `run_editor`'s job, and it needs them in order to do it.
    """
    turns: list[Turn] = []
    for block in getattr(message, "content", ()) or ():
        text = getattr(block, "text", None)
        if isinstance(text, str):
            turns.append(text)

    usage = getattr(message, "usage", None)
    if usage is not None:
        turns.append(_observed(usage))
    return turns


def _observed(usage: object) -> TokenConsumption:
    """One SDK usage payload, translated into the harness's three buckets.

    Claude's `input_tokens` already excludes both cached halves, so no subtraction is needed — but
    `cache_creation_input_tokens` is folded in, because a cache *write* is prompt the model paid
    close to full price for, and the bucket boundary that matters is the one against cache reads.
    """

    def count(name: str) -> int:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
        return value if isinstance(value, int) else 0

    return TokenConsumption.split(
        input=count("input_tokens") + count("cache_creation_input_tokens"),
        cache_read=count("cache_read_input_tokens"),
        output=count("output_tokens"),
    )


def claude_sdk_session(ask: TurnStreamAsk) -> TurnStreamSession:
    return _SdkSession(ask=ask)
