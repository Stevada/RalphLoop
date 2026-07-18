"""Copilot as an SDK-backed Implementer and Editor.

Keeping two CLIs on each side is a **portfolio decision, not a hedge**: the Implementer and the
Editor should not be the same model on the same failure, because an Editor adjudicating an impasse
declared by *itself* is the least independent sensor the system could have. `cli.py` picks; this
module is picked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ralph.adapters.copilot_sdk_session import copilot_sdk_session
from ralph.adapters.editor import (
    TurnStreamAsk,
    TurnStreamSession,
    OpenSession,
    Permission,
    read_only,
    run_editor,
    run_turn_stream,
)
from ralph.adapters.prompt import editor_prompt, implementer_prompt
from ralph.adapters.session import implementer_telemetry
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.ports import SessionContext


# ── the two actors ───────────────────────────────────────────────────────────────────────────


def allow_all(_tool: str, _input: dict[str, object]) -> Permission:
    """The Implementer is allowed to write code and commit it."""
    return Permission(True)


@dataclass(frozen=True, slots=True)
class CopilotImplementer:
    """Copilot writing code through the SDK session."""

    open_session: OpenSession

    async def run(self, context: SessionContext) -> SessionTelemetry:
        session = self.open_session(
            TurnStreamAsk(
                prompt=implementer_prompt(context.brief, context.findings),
                cwd=context.worktree.path,
                permit=allow_all,
            )
        )
        return await run_implementer(session, context)


async def run_implementer(
    session: TurnStreamSession, context: SessionContext
) -> SessionTelemetry:
    completed = await run_turn_stream(session, context.budget)
    return implementer_telemetry(
        bound=completed.bound,
        exit_code=completed.exit_code,
        output=completed.output,
        wall_clock_s=completed.wall_clock_s,
        worktree=context.worktree,
    )


def copilot_implementer() -> CopilotImplementer:
    return CopilotImplementer(open_session=copilot_sdk_session)


@dataclass(frozen=True, slots=True)
class CopilotEditor:
    """Copilot adjudicating a failed session, in the failed worktree, read-only."""

    open_session: OpenSession
    suite: Sequence[str]

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


def copilot_editor(suite: Sequence[str]) -> CopilotEditor:
    return CopilotEditor(open_session=copilot_sdk_session, suite=suite)
