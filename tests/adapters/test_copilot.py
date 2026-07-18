"""The Copilot adapter: the invocation, the log parser, and the read-only ask.

**No test here runs the real `copilot` binary.** The fixture at `tests/fixtures/copilot-debug.log`
is a genuine debug log from a real session, and the stub process below replays one. What is under
test is everything the harness does with that log — not whether GitHub's CLI keeps its promises.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

import pytest

from ralph.adapters.copilot import (
    READ_ONLY_TOOLS,
    CopilotEditor,
    CopilotLogError,
    copilot_argv,
    final_log_consumed_tokens,
    fresh_log_dir,
    log_dir_of,
    json_blocks,
    read_only_patterns,
    usage_of,
)
from ralph.adapters.git import GitCli
from ralph.adapters.session import SubprocessImplementer
from ralph.harness import (
    EditorVerdict,
    Outcome,
    SessionTelemetry,
    Verdict,
    failure_report,
)
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext, Worktree
from tests.builders import impasse, suite, telemetry
from tests.testbed import TargetRepo

FIXTURE = Path(__file__).parents[1] / "fixtures" / "copilot-debug.log"

SUITE: Sequence[str] = ("python", "-m", "pytest")

FAILURE = failure_report(
    Outcome.IMPASSE, telemetry(commits=0, impasse_report=impasse()), suite(green=True)
)


# ── the fixture is real, and it is the whole point ───────────────────────────────────────────


def test_the_real_log_yields_the_real_context() -> None:
    """A genuine Copilot session, parsed."""
    usages = [usage_of(b) for b in _blocks(FIXTURE.read_text())]
    found = [u for u in usages if u is not None]

    assert found == [(8_362, 8_366)]


def test_the_model_capabilities_block_is_not_mistaken_for_usage() -> None:
    """`usage_of` reads the parsed object's top-level `usage` key."""
    text = FIXTURE.read_text()
    assert "max_prompt_tokens" in text  # the trap is really in there
    capabilities = [b for b in _blocks(text) if "capabilities" in b]
    assert capabilities, "the fixture should contain the model-info block"
    assert all(usage_of(b) is None for b in capabilities)


def _blocks(text: str) -> list[dict[str, object]]:
    async def collect() -> list[dict[str, object]]:
        return [b async for b in json_blocks(_lines(text))]

    return asyncio.run(collect())


async def _lines(text: str) -> AsyncGenerator[str, None]:
    for line in text.split("\n"):
        yield line


# ── the block extractor ──────────────────────────────────────────────────────────────────────


def test_it_reassembles_a_pretty_printed_block() -> None:
    """The log is JSON *inside* a text log. `json.loads` per line finds nothing at all."""
    log = '2026-01-01T00:00:00Z [DEBUG] {\n  "usage": {\n    "prompt_tokens": 7,\n'
    log += '    "total_tokens": 9\n  }\n}\n'
    assert [usage_of(b) for b in _blocks(log)] == [(7, 9)]


def test_an_unbalanced_brace_inside_a_string_does_not_break_the_block() -> None:
    """**The brief is echoed into the log, and a brief is not JSON.**

    Copilot logs the whole prompt it was sent — which contains the sub-issue, which routinely
    contains a code fence. One `if (x) {` in a target repo's acceptance criteria is an unbalanced
    brace inside a JSON string, and a parser counting braces naively never finds the end of the
    block, because somebody's brief mentioned JavaScript.

    An escaped quote is the same hazard one level down — get `in_string` wrong and every brace
    after it is counted in the wrong régime.
    """
    brief = 'the handler opens with `if (x) {` — and mind the \\" quoting'
    log = f'2026-01-01T00:00:00Z [DEBUG] {{\n  "system": "{brief}",\n'
    log += '  "usage": {"prompt_tokens": 11, "total_tokens": 12}\n}\n'

    assert [usage_of(b) for b in _blocks(log)] == [(11, 12)]


def test_a_stray_closing_brace_in_prose_does_not_end_the_block_early() -> None:
    """The other half of the same bug, and the one that fails *quietly*: an early close leaves a
    fragment that happens to be valid JSON, so nothing raises — the usage block is simply never
    reached."""
    log = '2026-01-01T00:00:00Z [DEBUG] {\n  "system": "close the block with } to finish",\n'
    log += '  "usage": {"prompt_tokens": 13, "total_tokens": 14}\n}\n'

    assert [usage_of(b) for b in _blocks(log)] == [(13, 14)]


def test_prose_between_blocks_is_ignored() -> None:
    log = "2026-01-01T00:00:00Z [INFO] Starting Copilot CLI: 1.0.70\n"
    log += '2026-01-01T00:00:00Z [DEBUG] Got model info: {\n  "capabilities": {}\n}\n'
    log += "2026-01-01T00:00:00Z [ERROR] Started MCP client\n"
    log += '2026-01-01T00:00:00Z [DEBUG] {\n  "usage": {"prompt_tokens": 3, "total_tokens": 4}\n}\n'
    assert [usage_of(b) for b in _blocks(log)] == [None, (3, 4)]


def test_a_usage_block_with_no_numbers_is_loud() -> None:
    """A `usage` block the harness cannot read is not a zero-token session."""
    with pytest.raises(CopilotLogError):
        usage_of({"usage": {"prompt_tokens": None, "total_tokens": 4}})


async def test_implementer_consumption_comes_from_the_final_log_usage(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await _copilot_implementer(
        _completion(20_000, 20_100) + _completion(90_000, 999_999)
    ).run(
        SessionContext(
            brief=Brief(body="build it"),
            findings=Findings(body=""),
            worktree=wt,
            budget=Budget(wall_clock_s=20.0),
        )
    )

    assert not hasattr(t, "peak_context_tokens")
    assert t.consumed_tokens == 999_999


def _completion(context: int, total: int) -> str:
    body = json.dumps({"object": "chat.completion", "usage": {
        "prompt_tokens": context, "completion_tokens": total - context, "total_tokens": total,
    }}, indent=2)  # fmt: skip
    return f"2026-01-01T00:00:00.000Z [DEBUG] {body}\n"


def _copilot_implementer(log: str) -> SubprocessImplementer:
    def argv(brief: Brief, findings: Findings, worktree: Worktree) -> Sequence[str]:
        log_dir = fresh_log_dir(worktree)
        script = (
            "import pathlib, sys\n"
            f"pathlib.Path({str(log_dir)!r}, 'process-1.log').write_text({log!r})\n"
            "print('done')\n"
        )
        return [sys.executable, "-c", script]

    return SubprocessImplementer(
        build_argv=argv,
        final_consumed_tokens=lambda _session, worktree: final_log_consumed_tokens(
            log_dir_of(worktree)
        ),
    )


# ── the invocation ───────────────────────────────────────────────────────────────────────────


def test_debug_logging_is_set_never_assumed(tmp_path: Path) -> None:
    argv = copilot_argv("do it", tmp_path, mcp=())
    assert "--log-level" in argv
    assert argv[argv.index("--log-level") + 1] == "debug"


def test_mcp_is_disabled_by_name_and_not_merely_the_builtin(tmp_path: Path) -> None:
    """`--disable-builtin-mcps` turns off `github-mcp-server` and nothing else."""
    argv = copilot_argv("do it", tmp_path, mcp=("azure", "context7"))
    assert "--disable-builtin-mcps" in argv
    assert argv.count("--disable-mcp-server") == 2
    assert "azure" in argv and "context7" in argv


def test_the_implementer_may_use_every_tool(tmp_path: Path) -> None:
    argv = copilot_argv("do it", tmp_path, mcp=(), allow_all=True)
    assert "--allow-all-tools" in argv
    assert not any(a.startswith("--available-tools") for a in argv)


def test_the_editor_gets_an_allowlist_of_four_tools_and_no_more(tmp_path: Path) -> None:
    """`create` and `edit` are denied by **absence** — the same shape as the SDK Editor's, and for
    the same reason: a blocklist fails open on the tool nobody thought of."""
    argv = copilot_argv("adjudicate", tmp_path, mcp=(), tools=READ_ONLY_TOOLS, allow=("shell(ls)",))
    assert "--available-tools=view,glob,grep,bash" in argv
    assert "create" not in "".join(argv)
    assert "edit" not in "".join(a for a in argv if a.startswith("--available-tools"))


def test_the_editor_never_gets_allow_all_tools(tmp_path: Path) -> None:
    """Not passing it is itself enforcement: without `--allow-all-tools` Copilot denies any shell
    command that was not allowed by name, and refuses shell redirection outright. That is what
    closes the `git log > evidence.txt` hole, which no per-command check can see."""
    argv = copilot_argv("adjudicate", tmp_path, mcp=(), tools=READ_ONLY_TOOLS)
    assert "--allow-all-tools" not in argv
    assert "--deny-tool=write" in argv


def test_the_editor_may_run_the_suite_and_read_the_repo() -> None:
    patterns = read_only_patterns(SUITE)
    assert "shell(python -m pytest)" in patterns
    assert "shell(python -m pytest:*)" in patterns  # it will want to narrow to the failing test
    assert "shell(git diff)" in patterns
    assert "shell(grep)" in patterns


def test_the_editor_may_not_commit_or_write() -> None:
    """The allowlist is shared with the SDK Editor: `READ_ONLY_GIT` has one home, and `commit` was
    never in it."""
    patterns = read_only_patterns(SUITE)
    for forbidden in ("shell(git commit)", "shell(git cherry-pick)", "shell(git push)",
                      "shell(git apply)", "shell(git reset)", "shell(tee)", "shell(sed)"):
        assert forbidden not in patterns  # fmt: skip


# ── the log directory ────────────────────────────────────────────────────────────────────────


def test_the_log_lands_beside_the_worktree_never_inside_it(tmp_path: Path) -> None:
    """A session told to commit its work would sweep its own debug log into the diff with the first
    `git add -A`, and the merge queue would read it."""
    wt = _worktree(tmp_path)
    log_dir = fresh_log_dir(wt)
    assert wt.path not in log_dir.parents
    assert log_dir.is_dir()


def test_a_revised_cycle_does_not_read_the_last_cycle_s_log(tmp_path: Path) -> None:
    """A revise re-cuts **the same branch at the same path**, so last cycle's log is sitting exactly
    where this cycle's is about to be looked for."""
    wt = _worktree(tmp_path)
    stale = fresh_log_dir(wt) / "process-old.log"
    stale.write_text(_completion(119_000, 119_100))

    again = fresh_log_dir(wt)

    assert list(again.glob("*.log")) == []


def _worktree(tmp_path: Path) -> Worktree:
    path = tmp_path / ".worktrees" / "active" / "02-thing"
    path.mkdir(parents=True)
    return Worktree(path=path, branch="ralph/02-thing", base="integration")


# ── the whole Editor, against a stub `copilot` ───────────────────────────────────────────────


async def test_the_editor_runs_to_completion(tmp_path: Path) -> None:
    telemetry, verdict = await _adjudicate(tmp_path, context=130_000, verdict=None)

    assert telemetry.killed is None
    assert not hasattr(telemetry, "peak_context_tokens")
    assert verdict is None


async def test_the_editor_reports_no_commits_by_construction(tmp_path: Path) -> None:
    """Not observed — *constructed*. It was denied every tool that could have made one."""
    telemetry, verdict = await _adjudicate(tmp_path, context=9_000, verdict=Verdict.REVISE)

    assert telemetry.commits == 0
    assert telemetry.diffstat == ""
    assert telemetry.killed is None
    assert verdict is not None and verdict.verdict is Verdict.REVISE


async def test_the_editor_reads_the_verdict_out_of_a_real_session(tmp_path: Path) -> None:
    _, verdict = await _adjudicate(tmp_path, context=9_000, verdict=Verdict.PLANNING_DEFECT)

    assert verdict is not None
    assert verdict.verdict is Verdict.PLANNING_DEFECT
    assert verdict.rationale.startswith("the brief assumed")


async def test_the_prompt_reaches_the_model_and_the_log_dir_reaches_the_cli(tmp_path: Path) -> None:
    """The two things the harness must actually deliver. A `--log-dir` that never arrived is a
    session with no usage telemetry; a brief that never arrived is a session adjudicating nothing."""
    seen: list[tuple[str, Path]] = []
    await _adjudicate(tmp_path, context=9_000, verdict=Verdict.REVISE, seen=seen)

    prompt, log_dir = seen[0]
    assert "You are the **Editor**" in prompt
    assert "build it" in prompt  # the brief the Implementer was given
    assert log_dir.is_dir()


async def _adjudicate(
    tmp_path: Path,
    *,
    context: int,
    verdict: Verdict | None,
    seen: list[tuple[str, Path]] | None = None,
) -> tuple[SessionTelemetry, EditorVerdict | None]:
    """The whole Editor, against a stub `copilot`: a process that writes a real debug log and says
    a real verdict, through the same seam the real one is wired through in `cli.py`."""
    said = ""
    if verdict is not None:
        answer: dict[str, str] = {
            "verdict": verdict.value,
            "rationale": "the brief assumed a module that is not there",
        }
        if verdict is Verdict.REVISE:
            # Only a `revise` may carry one. A terminal verdict with a brief attached is a
            # contradiction the harness refuses to construct — nothing would ever read it.
            answer["revised_brief"] = "do it again, better"
        said = f"<verdict>{json.dumps(answer)}</verdict>"

    def argv(prompt: str, log_dir: Path) -> Sequence[str]:
        if seen is not None:
            seen.append((prompt, log_dir))
        script = (
            f"import pathlib\n"
            f"pathlib.Path({str(log_dir)!r}, 'process-1.log')"
            f".write_text({_completion(context, context + 50)!r})\n"
            f"print({said!r})\n"
        )
        return [sys.executable, "-c", script]

    return await CopilotEditor(argv=argv).adjudicate(
        SessionContext(
            brief=Brief(body="build it"),
            findings=Findings(body=""),
            worktree=_worktree(tmp_path),
            budget=Budget(wall_clock_s=20.0),
        ),
        FAILURE,
        must_be_terminal=False,
    )
