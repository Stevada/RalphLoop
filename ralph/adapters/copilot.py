"""Copilot, as either actor. One invocation and one log parser, backing both roles.

Keeping two CLIs on each side is a **portfolio decision, not a hedge**: the Implementer and the
Editor should not be the same model on the same failure, because an Editor adjudicating an impasse
declared by *itself* is the least independent sensor the system could have. `cli.py` picks; this
module is picked.

**The context signal is in the debug log, and only there.** Copilot *does* have an event stream
(`--output-format json`) — the contract used to say it did not — but that stream publishes
`outputTokens`, the completion count, and never once mentions the prompt. It is precisely the one
number the ceiling does not want. The number the ceiling *does* want is in the `--log-dir` log, in
a pretty-printed `usage` block, and only at `--log-level debug`.

Three things follow, and each one is a way the ceiling silently stops working:

- **`--log-level debug` is required.** At the default level there are no `usage` blocks at all —
  verified against thirty-five logs on this machine, not one of which had one. A session that
  publishes no observation is not a cheap session; it is an unmetered one, and this adapter raises
  rather than let it run to the wall clock unwatched.
- **The log is pretty-printed JSON inside a timestamped text log**, not JSONL, so it needs a
  brace-matched block extractor. And the block must be *parsed* before it is trusted: the same log
  carries a model-capabilities block containing `max_prompt_tokens: 200000`, so a parser that went
  looking for the string `prompt_tokens` would read the model's context *limit* as its context
  *usage* and kill every session before its first turn.
- **Copilot loads the world into the prompt before it reads the brief.** Measured here: **56.5k**
  of context to answer the word "pong" — 47% of the smart zone, gone at turn zero. MCP servers are
  most of it. With MCP disabled: **25.9k**. With the tool list also cut to what an Editor may hold:
  **8.4k**. So the harness disables MCP on every Copilot run, because a ceiling that fires on work
  which never had room to begin with is not a quality bound, it is a tax.

Unlike Codex, a Copilot session has no thread id to announce and no shared directory to find itself
in: it is *given* a private `--log-dir`. There is nothing to disambiguate, so there is no stdout
handshake here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ralph.adapters.context import tail
from ralph.adapters.editor import READ_ONLY_COMMANDS, READ_ONLY_GIT, editor_telemetry, verdict_of
from ralph.adapters.prompt import editor_prompt, implementer_prompt
from ralph.adapters.session import Session, SubprocessImplementer, Transcript, run_session
from ralph.harness import EditorVerdict, FailureReport, SessionTelemetry
from ralph.issues import Brief, Findings
from ralph.ports import Observation, SessionContext, Worktree

log = logging.getLogger(__name__)

COPILOT = "copilot"
MODEL = "gpt-5.3-codex"
POLL_S = 0.05

USAGE = "usage"
PROMPT_TOKENS = "prompt_tokens"
TOTAL_TOKENS = "total_tokens"

VIEW, GLOB, GREP, BASH = "view", "glob", "grep", "bash"
READ_ONLY_TOOLS = (VIEW, GLOB, GREP, BASH)
"""Copilot's names for the four tools an Editor may hold — the same four the SDK Editor holds as
`Read`, `Glob`, `Grep`, `Bash`. `create` and `edit` are denied by **absence**: `--available-tools`
is an allowlist, and Copilot honours it by never sending the others to the model. Verified against
a real session's wire request, where they were simply not there."""


class CopilotLogError(RuntimeError):
    """A Copilot session published no context signal.

    Loud, and it must stay loud. The tempting reading is "that session was cheap"; the true one is
    that nothing was watching the ceiling. Almost always the cause is a log written without
    `--log-level debug` — which is exactly the failure the ceiling cannot detect for itself.
    """


# ── the invocation, shared by both roles ─────────────────────────────────────────────────────


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


def suite_patterns(suite: Sequence[str]) -> tuple[str, ...]:
    """The one thing the Editor may run that *executes*: the repo's own suite.

    Both an exact match and a prefix match, because the Editor will want to narrow the run down to
    the failing test and the harness cannot know in advance what it will append.
    """
    command = " ".join(suite)
    return (f"shell({command})", f"shell({command}:*)")


def read_only_patterns(suite: Sequence[str]) -> tuple[str, ...]:
    """Copilot's spelling of the read-only allowlist that `adapters/editor.py` owns.

    The **contents** are not redefined here — `READ_ONLY_COMMANDS` and `READ_ONLY_GIT` are the same
    frozensets the SDK Editor's permission callback consults. What counts as read-only is one piece
    of knowledge with one home; this function only translates it into `shell(...)` patterns. If the
    two lists were maintained separately they would drift, and the day they drifted the two Editors
    would no longer be running under the same rules.
    """
    git = tuple(f"shell(git {sub})" for sub in sorted(READ_ONLY_GIT))
    commands = tuple(f"shell({cmd})" for cmd in sorted(READ_ONLY_COMMANDS))
    return suite_patterns(suite) + git + commands


def copilot_argv(
    prompt: str,
    log_dir: Path,
    *,
    mcp: Sequence[str],
    tools: Sequence[str] | None = None,
    allow: Sequence[str] = (),
    allow_all: bool = False,
) -> list[str]:
    """One command line, both actors. What differs is only what the session may touch.

    `allow_all=True` is the Implementer: it is here to write code. `tools` + `allow` is the Editor.
    **Not passing `--allow-all-tools` is itself part of the enforcement**: without it Copilot denies
    any shell command that was not explicitly allowed, and refuses shell redirection outright —
    which is what closes the `git log > evidence.txt` hole that no per-command check can see.
    """
    argv = [
        COPILOT,
        "-p",
        prompt,
        "--model",
        MODEL,
        "--log-dir",
        str(log_dir),
        "--log-level",
        "debug",  # without this there are no usage blocks and the ceiling is blind
        "--no-color",
        "--disable-builtin-mcps",
    ]
    for server in mcp:
        argv += ["--disable-mcp-server", server]
    if tools is not None:
        argv.append(f"--available-tools={','.join(tools)}")
    if allow_all:
        argv.append("--allow-all-tools")
    else:
        # Denial beats every allow in Copilot's engine, including an `--allow-all-tools` that a
        # later edit puts on this command line by mistake. It costs one argument to say it twice.
        argv.append("--deny-tool=write")
        argv += [f"--allow-tool={pattern}" for pattern in allow]
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
    from being mistaken for a session that has already blown twice through the ceiling.
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


@dataclass(slots=True)
class CopilotContextSource:
    """Tails the one log in the session's own log directory, an observation per model call.

    `consumed_tokens` is **accumulated here**, and that is not a detail. Codex reports a running
    total; Copilot reports `total_tokens` for the call in hand (25,885 + 4 = 25,889, and the next
    call starts over). The port's contract is cumulative spend, so the adapter holding the per-call
    number is the one that has to add it up — otherwise `ContextMeter`, which takes a `max()` over
    what it is handed, would report the largest single call as the whole session's cost.
    """

    transcript: Transcript
    log_dir: Path
    poll_s: float = POLL_S
    _spent: int = field(default=0, init=False)

    async def observations(self) -> AsyncGenerator[Observation, None]:
        path = await self._log()
        if path is None:
            raise CopilotLogError(f"copilot wrote no log to {self.log_dir}. It ran unmetered.")

        seen = 0
        lines = tail(path, until=self.transcript.closed, poll_s=self.poll_s)
        async for block in json_blocks(lines):
            usage = usage_of(block)
            if usage is None:
                continue
            context, spent = usage
            self._spent += spent
            seen += 1
            yield Observation(
                context_tokens=context,
                consumed_tokens=self._spent,
                rate_limit_used_percent=None,  # copilot publishes none
            )

        if seen == 0:
            raise CopilotLogError(
                f"{path} carries no usage block, so nothing metered this session's context. "
                "The usual cause is a log written without `--log-level debug`."
            )

    async def _log(self) -> Path | None:
        """The session owns its log directory, so the only question is *when* the file turns up."""
        while True:
            found = sorted(self.log_dir.glob("*.log"))
            if found:
                return found[0]
            if self.transcript.closed.is_set():
                return None
            await asyncio.sleep(self.poll_s)


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


EditorArgv = Callable[[str, Path], Sequence[str]]
"""How this Editor is spelled on a command line: a prompt and a log directory in, an argv out.

The seam the tests drive a stub `copilot` through — the same shape as `ClaudeCodeEditor`'s
`OpenSession`, and for the same reason: no test in this suite may depend on a model answering.
"""


@dataclass(frozen=True, slots=True)
class CopilotEditor:
    """Copilot adjudicating a failed session, in the failed worktree, read-only.

    **This Editor's read-only guarantee is weaker than the SDK Editor's, and the difference is not
    that one list is longer.** It is that `ClaudeCodeEditor`'s enforcement is a pure function the
    harness owns and the suite tests fifty ways; this one's is a permission engine inside a binary
    we do not control, cannot inspect, and do not exercise in any test. What is checked here is that
    the harness *asks* correctly — an allowlist of four tools, an allowlist of shell commands, no
    `--allow-all-tools`, and `--deny-tool=write` on top. That Copilot then *honours* the ask is an
    assumption. It is a reasonable one, and it is still an assumption. Prefer the SDK Editor where
    the choice is free; this exists so the Editor need not be the same model as the Implementer.
    """

    argv: EditorArgv

    async def adjudicate(
        self,
        context: SessionContext,
        failure: FailureReport,
        must_be_terminal: bool,
    ) -> tuple[SessionTelemetry, EditorVerdict | None]:
        worktree = context.worktree
        log_dir = fresh_log_dir(worktree)
        session = await run_session(
            self.argv(
                editor_prompt(context.brief, context.findings, failure, must_be_terminal), log_dir
            ),
            worktree.path,  # the failed worktree, exactly as the Implementer left it
            context.budget,
            lambda transcript: CopilotContextSource(transcript=transcript, log_dir=log_dir),
        )
        telemetry = editor_telemetry(
            bound=session.bound,
            exit_code=session.exit_code,
            output=session.output,
            wall_clock_s=session.wall_clock_s,
        )
        # `must_be_terminal` was told to the model and is enforced nowhere near it: a third-cycle
        # `revise` is refused by the *scheduler*. One rule, one home.
        return telemetry, verdict_of(session.output)


def copilot_editor(suite: Sequence[str]) -> CopilotEditor:
    mcp = mcp_servers()
    allow = read_only_patterns(suite)

    def argv(prompt: str, log_dir: Path) -> Sequence[str]:
        return copilot_argv(prompt, log_dir, mcp=mcp, tools=READ_ONLY_TOOLS, allow=allow)

    return CopilotEditor(argv=argv)
