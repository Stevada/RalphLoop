"""Codex's two actors over the JSONL turn stream."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ralph.adapters.codex.session import codex_read_only_session, codex_sdk_session
from ralph.adapters.runtime.editor import run_editor
from ralph.adapters.runtime.prompt import (
    conflict_resolution_prompt,
    editor_prompt,
    implementer_prompt,
)
from ralph.adapters.runtime.implementer import run_turn_stream_implementer
from ralph.adapters.runtime.turn_stream import OpenSession, TurnStreamAsk
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.ports import SessionContext


@dataclass(frozen=True, slots=True)
class CodexImplementer:
    """Codex writing code through the JSONL turn stream."""

    open_session: OpenSession

    async def run(self, context: SessionContext) -> SessionTelemetry:
        session = self.open_session(
            TurnStreamAsk(
                prompt=implementer_prompt(context.spec, context.findings),
                cwd=context.worktree.path,
            )
        )
        return await run_turn_stream_implementer(session, context)

    async def resolve_conflict(
        self, context: SessionContext, resumable_identifier: str
    ) -> SessionTelemetry:
        session = self.open_session(
            TurnStreamAsk(
                prompt=conflict_resolution_prompt(),
                cwd=context.worktree.path,
                resumable_identifier=resumable_identifier,
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
                    context.spec, context.findings, failure, must_be_terminal
                ),
                cwd=context.worktree.path,
            )
        )
        return await run_editor(session, context.budget)


def codex_implementer() -> CodexImplementer:
    return CodexImplementer(open_session=codex_sdk_session)


def codex_editor(suite: Sequence[str]) -> CodexEditor:
    return CodexEditor(open_session=codex_read_only_session, suite=suite)
