"""Codex as an Implementer: `codex exec`, bounded by the smart zone.

**The context signal is not on stdout.** `codex exec --json` emits `turn.completed` — which
carries `usage` — only at session end, because one `exec` invocation is a single turn, tool calls
included. Waiting for it would mean metering a session after it was already too late to stop.

What *is* live is the session **rollout file**, to which Codex appends a `token_count` event after
every model call:

    {"type": "event_msg", "payload": {"type": "token_count", "info": {
       "last_token_usage":  {"input_tokens": 16802},   <- the context. THE CEILING IS ON THIS.
       "total_token_usage": {"total_tokens": 33410},   <- consumption. telemetry only.
       "model_context_window": 272000
     }, "rate_limits": {"primary": {"used_percent": 0.0}}}}

So this tails the rollout file, not stdout. Stdout is still needed for one thing: the `thread_id`
Codex announces on its first line, which is the only link between *this* process and *its* rollout
file. A run has four Codex sessions open at once by default, and metering a sibling's file would
kill the wrong session.

The ceiling (120k) sits far below the window (272k), so it always fires **before** Codex would
auto-compact — compaction never gets the chance to drop the context back under the bound and hide
the crossing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ralph.adapters.context import tail
from ralph.adapters.prompt import implementer_prompt
from ralph.adapters.session import SubprocessImplementer, Transcript
from ralph.issues import Brief, Findings
from ralph.ports import Observation, Worktree

log = logging.getLogger(__name__)

MODEL_ENV = "CODEX_MODEL"
SANDBOX_ENV = "CODEX_SANDBOX"
APPROVAL_ENV = "CODEX_APPROVAL"
UNSANDBOXED_ENV = "RALPH_CODEX_UNSANDBOXED"
HOME_ENV = "CODEX_HOME"

DEFAULT_MODEL = "gpt-5.3-codex"
DEFAULT_SANDBOX = "workspace-write"
DEFAULT_APPROVAL = "never"

THREAD_STARTED = "thread.started"
TOKEN_COUNT = "token_count"
POLL_S = 0.05


class RolloutParseError(ValueError):
    """A `token_count` event the harness cannot read.

    Fatal, and deliberately so. The alternative — shrugging and reading on — is a ceiling that
    silently never fires, which is worse than no ceiling at all: the harness would go on reporting
    a peak of zero for sessions that had left the smart zone hours ago.
    """


def codex_sessions_dir() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".codex") / "sessions"


def codex_argv(brief: Brief, findings: Findings, worktree: Worktree) -> Sequence[str]:
    """`--json` is not optional: it is how the session announces the thread id that finds its
    rollout file. Without it there is nothing to meter."""
    argv = [
        "codex",
        "exec",
        "--json",
        "--model",
        os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
    ]
    if os.environ.get(UNSANDBOXED_ENV) == "1":
        # Mutually exclusive with --sandbox/--ask-for-approval; Codex rejects them together.
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += [
            "--sandbox",
            os.environ.get(SANDBOX_ENV) or DEFAULT_SANDBOX,
            "--ask-for-approval",
            os.environ.get(APPROVAL_ENV) or DEFAULT_APPROVAL,
        ]
    argv.append(implementer_prompt(brief, findings))
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


def parse_observation(line: str) -> Observation | None:
    """A `token_count` event, or nothing.

    `last_token_usage.input_tokens` is the number the ceiling gates on: the context the model was
    reasoning over on its last call. `total_token_usage.total_tokens` sits directly beside it, is
    named almost the same, and is **consumption** — it climbs forever. A ceiling on that would kill
    a long, cheap, perfectly focused session and spare a bloated one.
    """
    payload = _obj(_loads(line).get("payload"))
    if payload.get("type") != TOKEN_COUNT:
        return None

    info = payload.get("info")
    if info is None:
        return None  # Codex emits a token_count with a null info before the first model call

    context = _obj(_obj(info).get("last_token_usage")).get("input_tokens")
    consumed = _obj(_obj(info).get("total_token_usage")).get("total_tokens")
    if not isinstance(context, int) or not isinstance(consumed, int):
        raise RolloutParseError(f"a token_count event with no usage in it: {line!r}")

    used = _obj(_obj(payload.get("rate_limits")).get("primary")).get("used_percent")
    return Observation(
        context_tokens=context,
        consumed_tokens=consumed,
        rate_limit_used_percent=float(used) if isinstance(used, int | float) else None,
    )


@dataclass(frozen=True, slots=True)
class CodexContextSource:
    """Tails *this* session's rollout file. Never a sibling's."""

    transcript: Transcript
    sessions_dir: Path
    poll_s: float = POLL_S

    async def observations(self) -> AsyncGenerator[Observation, None]:
        id = await self.transcript.first(thread_id)
        if id is None:
            log.warning("codex never announced a thread id; this session ran unmetered")
            return

        rollout = await self._rollout(id)
        if rollout is None:
            log.warning("no rollout file for thread %s; this session ran unmetered", id)
            return

        async for line in tail(rollout, until=self.transcript.closed, poll_s=self.poll_s):
            observation = parse_observation(line)
            if observation is not None:
                yield observation

    async def _rollout(self, id: str) -> Path | None:
        """Codex names the file after the thread, so the thread id is the whole of the lookup.

        It is written a moment after `thread.started` reaches us, so this waits for it — but only
        until the session ends. A session that dies before its first model call leaves no rollout
        file, and there is nothing to wait for.
        """
        while True:
            found = sorted(self.sessions_dir.glob(f"**/rollout-*{id}.jsonl"))
            if found:
                return found[0]
            if self.transcript.closed.is_set():
                return None
            await asyncio.sleep(self.poll_s)


def codex_implementer() -> SubprocessImplementer:
    """An Implementer is an argv and a context source. Codex is `codex exec` and a rollout tail."""
    sessions = codex_sessions_dir()
    return SubprocessImplementer(
        build_argv=codex_argv,
        # The worktree is no help to Codex: its rollout lives in a directory shared with every
        # other session on the machine, and the only thing that picks its own out of that pile is
        # the thread id it announces on stdout.
        context=lambda transcript, _worktree: CodexContextSource(
            transcript=transcript, sessions_dir=sessions
        ),
    )
