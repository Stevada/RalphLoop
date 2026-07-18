"""Codex as an Implementer: `codex exec`, bounded by the wall clock."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ralph.adapters.prompt import implementer_prompt
from ralph.adapters.session import Session, SubprocessImplementer
from ralph.issues import Brief, Findings
from ralph.ports import Worktree

MODEL = "gpt-5.3-codex"
SANDBOX = "workspace-write"
APPROVAL = "never"

THREAD_STARTED = "thread.started"
TURN_COMPLETED = "turn.completed"


class CodexUsageError(ValueError):
    """A completed Codex turn whose token usage the harness cannot read."""


def codex_argv(brief: Brief, findings: Findings, worktree: Worktree) -> Sequence[str]:
    """`--json` is not optional: it is how the session reports completed-turn usage."""
    del worktree
    argv = [
        "codex", "exec", "--json",
        "--model", MODEL,
        "--sandbox", SANDBOX,
        "--ask-for-approval", APPROVAL,
        implementer_prompt(brief, findings),
    ]
    return argv


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


async def end_of_turn_consumed_tokens(session: Session, _worktree: Worktree) -> int | None:
    """The total Codex reports when the `exec` turn completes, or none if it never completed."""
    found: int | None = None
    for line in session.output.splitlines():
        event = _loads(line)
        if event.get("type") != TURN_COMPLETED:
            continue
        total = _obj(event.get("usage")).get("total_tokens")
        if not isinstance(total, int):
            raise CodexUsageError(f"a turn.completed event with no total_tokens in it: {line!r}")
        found = total
    return found


def codex_implementer() -> SubprocessImplementer:
    """Codex is `codex exec` and the completed turn's usage."""
    return SubprocessImplementer(
        build_argv=codex_argv,
        final_consumed_tokens=end_of_turn_consumed_tokens,
    )
