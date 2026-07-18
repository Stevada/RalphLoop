"""Copilot as a CLI Implementer, plus the SDK-backed Editor.

Keeping two CLIs on each side is a **portfolio decision, not a hedge**: the Implementer and the
Editor should not be the same model on the same failure, because an Editor adjudicating an impasse
declared by *itself* is the least independent sensor the system could have. `cli.py` picks; this
module is picked.

The CLI Implementer's debug log contains completed usage blocks. The harness reads those at session
end for token-consumption telemetry.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ralph.adapters.copilot_sdk_session import copilot_sdk_session
from ralph.adapters.editor import Ask, OpenSession, read_only, run_editor
from ralph.adapters.prompt import editor_prompt, implementer_prompt
from ralph.adapters.session import Session, SubprocessImplementer
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.issues import Brief, Findings
from ralph.ports import SessionContext, Worktree

log = logging.getLogger(__name__)

COPILOT = "copilot"
MODEL = "gpt-5.3-codex"
POLL_S = 0.05

USAGE = "usage"
PROMPT_TOKENS = "prompt_tokens"
TOTAL_TOKENS = "total_tokens"


class CopilotLogError(RuntimeError):
    """A Copilot log whose usage payload the harness cannot read."""


# ── the command line ─────────────────────────────────────────────────────────────────────────


def mcp_servers() -> tuple[str, ...]:
    """Every MCP server Copilot would load, from every source it loads them from.

    `--disable-builtin-mcps` alone is not enough: it disables `github-mcp-server` and nothing else,
    while the servers actually costing us the prompt come from the user's config, the workspace,
    and installed plugins. Copilot will enumerate all of them if asked, so the harness asks rather
    than guessing at names it cannot know.
    """
    try:
        listed = subprocess.run(
            [COPILOT, "mcp", "list", "--json"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CopilotLogError(f"cannot ask copilot which MCP servers it loads: {exc}") from exc
    servers = json.loads(listed.stdout).get("mcpServers")
    return tuple(servers) if isinstance(servers, dict) else ()


def copilot_argv(
    prompt: str,
    log_dir: Path,
    *,
    mcp: Sequence[str],
    allow_all: bool = False,
) -> list[str]:
    """The Copilot CLI command line. `allow_all=True` is the Implementer: it is here to write code."""
    argv = [
        COPILOT,
        "-p",
        prompt,
        "--model",
        MODEL,
        "--log-dir",
        str(log_dir),
        "--log-level",
        "debug",
        "--no-color",
        "--disable-builtin-mcps",
    ]
    for server in mcp:
        argv += ["--disable-mcp-server", server]
    if allow_all:
        argv.append("--allow-all-tools")
    return argv


def log_dir_of(worktree: Worktree) -> Path:
    """Where a session's log lives: beside the worktree, and never inside it.

    Inside the checkout it would be an untracked file in a repository whose session has been told,
    in as many words, to commit its work — and the first `git add -A` would sweep Copilot's own
    debug log into the diff the merge queue reads.
    """
    return worktree.path.parent / f".{worktree.path.name}.copilot"


def fresh_log_dir(worktree: Worktree) -> Path:
    """The same directory, emptied.

    Emptied and not merely created, because a revised cycle re-cuts **the same branch at the same
    path** — so last cycle's log is sitting exactly where this cycle's is about to be looked for. A
    session metered against its predecessor's log is worse than one metered against nothing: it is
    a plausible number describing a session that no longer exists.
    """
    path = log_dir_of(worktree)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


# ── the log parser, shared by both roles ─────────────────────────────────────────────────────


async def json_blocks(lines: AsyncGenerator[str, None]) -> AsyncGenerator[dict[str, object], None]:
    """The pretty-printed JSON objects embedded in a text log, reassembled as they arrive.

    Copilot logs `2026-07-14T13:25:03.012Z [DEBUG] {` and then twenty indented lines of object. A
    block opens on a line *ending* in `{` and closes where the braces balance — counting only braces
    outside string literals, because these blocks carry whole system prompts, and that prose is full
    of both braces and escaped quotes.
    """
    depth = 0
    block: list[str] = []
    async for line in lines:
        if depth == 0:
            start = line.find("{")
            if start == -1 or line.rstrip().endswith("{") is False:
                continue
            line = line[start:]
        block.append(line)
        depth += _braces(line)
        if depth <= 0:
            text, block, depth = "\n".join(block), [], 0
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue  # a `{` that opened something which was never JSON. Not our business.
            if isinstance(parsed, dict):
                yield parsed


def _braces(line: str) -> int:
    """Net brace depth of a line, ignoring anything inside a string literal."""
    depth = 0
    in_string = False
    escaped = False
    for ch in line:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
    return depth


def usage_of(block: dict[str, object]) -> tuple[int, int] | None:
    """`(context, what this one call spent)`, or None if this block is not a completion.

    `usage` is read from the **parsed** object, at the top level. That is what keeps the
    model-capabilities block — which contains `capabilities.limits.max_prompt_tokens: 200000` —
    from being mistaken for a real usage block.
    """
    usage = block.get(USAGE)
    if not isinstance(usage, dict):
        return None
    context = usage.get(PROMPT_TOKENS)
    spent = usage.get(TOTAL_TOKENS)
    if not isinstance(context, int) or not isinstance(spent, int):
        raise CopilotLogError(f"a usage block with no token counts in it: {usage!r}")
    return context, spent


async def final_log_consumed_tokens(log_dir: Path) -> int | None:
    """The last completed usage total in Copilot's finished log, if the session reached one."""
    found: int | None = None
    for path in sorted(log_dir.glob("*.log")):
        async for block in json_blocks(_file_lines(path)):
            usage = usage_of(block)
            if usage is not None:
                _context, total = usage
                found = total
    return found


async def _file_lines(path: Path) -> AsyncGenerator[str, None]:
    for line in path.read_text().splitlines():
        yield line


# ── the two actors ───────────────────────────────────────────────────────────────────────────


def copilot_implementer() -> SubprocessImplementer:
    """Copilot with its hands free: every tool, a mandate to commit, and final log usage."""
    mcp = mcp_servers()

    def argv(brief: Brief, findings: Findings, worktree: Worktree) -> Sequence[str]:
        return copilot_argv(
            implementer_prompt(brief, findings),
            fresh_log_dir(worktree),
            mcp=mcp,
            allow_all=True,
        )

    async def consumed_tokens(_session: Session, worktree: Worktree) -> int | None:
        return await final_log_consumed_tokens(log_dir_of(worktree))

    return SubprocessImplementer(
        build_argv=argv,
        final_consumed_tokens=consumed_tokens,
    )


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
            Ask(
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
