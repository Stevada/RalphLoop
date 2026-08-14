"""Copilot's three actors: an Implementer that writes code, an Integrator that reconciles a
conflict, and an Editor that adjudicates — all on the SDK session in `session.py`.

Keeping two CLIs on each side is a **portfolio decision, not a hedge**: the Implementer and the
Editor should not be the same model on the same failure, because an Editor adjudicating an impasse
declared by *itself* is the least independent sensor the system could have. `cli.py` picks; this
module is picked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ralph.adapters.copilot.session import copilot_sdk_session
from ralph.adapters.runtime.editor import read_only, run_editor
from ralph.adapters.runtime.implementer import run_turn_stream_implementer
from ralph.adapters.runtime.integrator import run_turn_stream_integrator
from ralph.adapters.runtime.prompt import (
    editor_prompt,
    implementer_prompt,
    integrator_prompt,
)
from ralph.adapters.runtime.turn_stream import (
    OpenSession,
    Permission,
    TurnStreamAsk,
)
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.ports import SessionContext


# ── the two actors ───────────────────────────────────────────────────────────────────────────


def allow_all(_tool: str, _input: dict[str, object]) -> Permission:
    """The Implementer and the Integrator are both allowed to write code and commit it."""
    return Permission(True)


@dataclass(frozen=True, slots=True)
class CopilotImplementer:
    """Copilot writing code through the SDK session."""

    open_session: OpenSession

    async def run(self, context: SessionContext) -> SessionTelemetry:
        session = self.open_session(
            TurnStreamAsk(
                prompt=implementer_prompt(context.candidate.spec, context.candidate.findings),
                cwd=context.candidate.worktree.path,
                permit=allow_all,
            )
        )
        return await run_turn_stream_implementer(session, context)


def copilot_implementer() -> CopilotImplementer:
    return CopilotImplementer(open_session=copilot_sdk_session)


@dataclass(frozen=True, slots=True)
class CopilotIntegrator:
    """Copilot reconciling a conflicted worktree. A fresh session, not a resumed one — the conflict
    is fully described by the repository it is standing in."""

    open_session: OpenSession

    async def reconcile(self, context: SessionContext) -> SessionTelemetry:
        session = self.open_session(
            TurnStreamAsk(
                prompt=integrator_prompt(context.candidate.spec, context.candidate.findings),
                cwd=context.candidate.worktree.path,
                permit=allow_all,
            )
        )
        return await run_turn_stream_integrator(session, context)


def copilot_integrator() -> CopilotIntegrator:
    return CopilotIntegrator(open_session=copilot_sdk_session)


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
                    context.candidate.spec, context.candidate.findings, failure, must_be_terminal
                ),
                cwd=context.candidate.worktree.path,
                permit=lambda tool, input: read_only(tool, input, self.suite),
            )
        )
        return await run_editor(session, context.budget)


def copilot_editor(suite: Sequence[str]) -> CopilotEditor:
    return CopilotEditor(open_session=copilot_sdk_session, suite=suite)
