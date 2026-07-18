"""Claude Code as the Editor: `claude-agent-sdk` running Opus, read-only, enforced by the harness.

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
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass

from ralph.adapters.editor import (
    TurnStreamAsk,
    OpenSession,
    READ_ONLY_TOOLS,
    TurnStreamSession,
    TokenUsage,
    Turn,
    read_only,
    run_editor,
)
from ralph.adapters.prompt import editor_prompt
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.ports import SessionContext

MODEL = "claude-opus-4-8"
"""The Editor is the expensive one on purpose. It runs at most three times per sub-issue and it is
the only actor whose judgment the harness cannot check against a suite."""


@dataclass(frozen=True, slots=True)
class ClaudeCodeEditor:
    """The Editor as the rest of the harness sees it: a brief in, a verdict out.

    It does not write the verdict to the run log, store the revised brief, spend a cycle, or decide
    what a `revise` on the final cycle means. All of that is the scheduler's, and keeping it there
    is why this class is twenty lines.
    """

    open_session: OpenSession
    suite: Sequence[str]
    """The repo's own test command — the one the harness itself runs. The Editor is allowed to run
    exactly this and nothing else that executes, which is how "re-run the suite in the failed
    worktree" and "you may not write to it" are both true at once."""

    async def adjudicate(
        self,
        context: SessionContext,
        failure: FailureReport,
        must_be_terminal: bool,
    ) -> tuple[SessionTelemetry, EditorVerdict | None]:
        session = self.open_session(
            TurnStreamAsk(
                prompt=editor_prompt(
                    context.brief, context.findings, failure, must_be_terminal
                ),
                cwd=context.worktree.path,
                permit=lambda tool, input: read_only(tool, input, self.suite),
            )
        )
        return await run_editor(session, context.budget)


# ── the far side of the seam ─────────────────────────────────────────────────────────────────


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
        # Imported here, not at module scope: `claude-agent-sdk` is an optional dependency, and a
        # run that never reaches Claude adjudication must not require it to be installed.
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            PermissionResultAllow,
            PermissionResultDeny,
            query,
        )

        async def can_use_tool(tool: str, input: dict[str, object], context: object) -> object:
            """**The enforcement surface.** The harness adjudicates the call before it happens."""
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
        try:
            async for message in query(prompt=self._ask.prompt, options=options):
                for turn in _turns_of(message):
                    self._turns.put_nowait(turn)
            self._code = 0
        except asyncio.CancelledError:
            self._code = -9
        finally:
            self._turns.put_nowait(None)

    @property
    def returncode(self) -> int | None:
        return self._code

    async def turns(self) -> AsyncGenerator[Turn, None]:
        while (turn := await self._turns.get()) is not None:
            yield turn

    def kill(self) -> None:
        if not self._running.done():
            self._running.cancel()

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


def _observed(usage: object) -> TokenUsage:
    """Token consumption from one SDK usage payload."""

    def count(name: str) -> int:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
        return value if isinstance(value, int) else 0

    prompt = (
        count("input_tokens")
        + count("cache_read_input_tokens")
        + count("cache_creation_input_tokens")
    )
    return TokenUsage(consumed_tokens=prompt + count("output_tokens"))


def claude_sdk_session(ask: TurnStreamAsk) -> TurnStreamSession:
    return _SdkSession(ask=ask)
