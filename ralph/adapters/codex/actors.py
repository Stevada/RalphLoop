"""Codex's two actors over the JSONL turn stream."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ralph.adapters.codex.session import codex_read_only_session, codex_sdk_session
from ralph.adapters.editor import run_editor
from ralph.adapters.prompt import editor_prompt, implementer_prompt
from ralph.adapters.session import run_turn_stream_implementer
from ralph.adapters.turn_stream import OpenSession, TurnStreamAsk
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.ports import SessionContext


@dataclass(frozen=True, slots=True)
class CodexImplementer:
    """Codex writing code through the JSONL turn stream."""

    open_session: OpenSession

    async def run(self, context: SessionContext) -> SessionTelemetry:
        session = self.open_session(
            TurnStreamAsk(
                prompt=implementer_prompt(context.brief, context.findings),
                cwd=context.worktree.path,
            )
        )
        return await run_turn_stream_implementer(session, context)


@dataclass(frozen=True, slots=True)
class CodexEditor:
    """Codex adjudicating a failed session under the Codex read-only sandbox."""

    open_session: OpenSession
    suite: Sequence[str]

    async def adjudicate(
        self,
        context: SessionContext,
        failure: FailureReport,
        must_be_terminal: bool,
    ) -> tuple[SessionTelemetry, EditorVerdict | None]:
        _ = self.suite  # Codex has no pre-tool callback; read-only is the sandbox mechanism.
        session = self.open_session(
            TurnStreamAsk(
                prompt=editor_prompt(
                    context.brief, context.findings, failure, must_be_terminal
                ),
                cwd=context.worktree.path,
            )
        )
        return await run_editor(session, context.budget)


def codex_implementer() -> CodexImplementer:
    return CodexImplementer(open_session=codex_sdk_session)


def codex_editor(suite: Sequence[str]) -> CodexEditor:
    return CodexEditor(open_session=codex_read_only_session, suite=suite)
