"""Claude Code as the Editor role."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ralph.adapters.claude.session import claude_sdk_session
from ralph.adapters.runtime.editor import read_only, run_editor
from ralph.adapters.runtime.prompt import editor_prompt
from ralph.adapters.runtime.turn_stream import OpenSession, TurnStreamAsk
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.ports import SessionContext


@dataclass(frozen=True, slots=True)
class ClaudeCodeEditor:
    """The Editor as the rest of the harness sees it: a spec in, a verdict out.

    It does not write the verdict to the run log, store the revised spec, spend a cycle, or decide
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
                    context.spec, context.findings, failure, must_be_terminal
                ),
                cwd=context.worktree.path,
                permit=lambda tool, input: read_only(tool, input, self.suite),
            )
        )
        return await run_editor(session, context.budget)


def claude_editor(suite: Sequence[str]) -> ClaudeCodeEditor:
    return ClaudeCodeEditor(open_session=claude_sdk_session, suite=suite)
