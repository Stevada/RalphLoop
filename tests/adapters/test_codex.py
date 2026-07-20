"""The Codex adapter: the argv it builds and the usage it reads at session end."""

from __future__ import annotations

import os
import asyncio
import json
import shutil
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

import pytest

from ralph.adapters.codex import (
    EDITOR_SANDBOX,
    IMPLEMENTER_SANDBOX,
    CodexUsageError,
    CodexEditor,
    CodexImplementer,
    CodexJsonSession,
    codex_argv,
    codex_implementer,
    end_of_turn_consumed_tokens,
)
from ralph.adapters.bounding import Bound
from ralph.adapters.git import GitCli
from ralph.adapters.session import Session
from ralph.adapters.turn_stream import AutoCompaction, TokenUsage, Turn, TurnStreamAsk
from ralph.harness import (
    Outcome,
    SuiteResult,
    Verdict,
    classify_editor,
    classify_implementer,
    failure_report,
)
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext, Worktree
from tests.builders import impasse, suite, telemetry
from tests.testbed import TargetRepo

BRIEF = Brief(body="# 01 — make it add\n\n## Acceptance criteria\n\n- [ ] `add(1, 2) == 3`")
FINDINGS = Findings(body="`add()` is already in calculator.py")
GENEROUS = Budget(wall_clock_s=30.0)
GREEN = SuiteResult(green=True, output="", duration_s=0.0)


def session_context(
    wt: Worktree, brief: Brief = BRIEF, findings: Findings = FINDINGS, budget: Budget = GENEROUS
) -> SessionContext:
    return SessionContext(brief=brief, findings=findings, worktree=wt, budget=budget)


# ── the argv ─────────────────────────────────────────────────────────────────────────────────


def test_the_session_is_asked_for_json() -> None:
    """`--json` is how the session reports completed-turn usage."""
    argv = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="main"))

    assert argv[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert "--json" in argv
    assert "--cd" in argv and "/w" in argv


def test_the_brief_and_the_findings_both_reach_the_model() -> None:
    prompt = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))[-1]

    assert "`add(1, 2) == 3`" in prompt
    assert "already in calculator.py" in prompt
    assert "<impasse>" in prompt


def test_how_codex_is_driven_is_fixed_not_configured() -> None:
    argv = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))

    assert "--sandbox" in argv and IMPLEMENTER_SANDBOX in argv
    assert "--ask-for-approval" in argv and "never" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "gpt-5.3-codex" in argv


# ── completed-turn usage ─────────────────────────────────────────────────────────────────────


async def test_consumption_comes_from_the_completed_turn_usage() -> None:
    session = Session(
        bound=Bound(killed=None, consumed_tokens=0),
        exit_code=0,
        output=(
            '{"type": "turn.completed", "usage": {"total_tokens": 123}}\n'
            '{"type": "turn.completed", "usage": {"total_tokens": 456}}\n'
        ),
        wall_clock_s=1.0,
    )

    assert await end_of_turn_consumed_tokens(session, Worktree(Path("/w"), "b", "base")) == 456


async def test_a_completed_turn_without_usage_is_loud() -> None:
    session = Session(
        bound=Bound(killed=None, consumed_tokens=0),
        exit_code=0,
        output='{"type": "turn.completed", "usage": {}}\n',
        wall_clock_s=1.0,
    )

    with pytest.raises(CodexUsageError):
        await end_of_turn_consumed_tokens(session, Worktree(Path("/w"), "b", "base"))


# ── the JSONL turn stream ────────────────────────────────────────────────────────────────────

STUB_CODEX = """\
import json, signal, sys, time

final = int(sys.argv[1])
mode = sys.argv[2]
if final:
    print(json.dumps({"type": "turn.completed", "usage": {"total_tokens": final}}), flush=True)
print(json.dumps({"type": "agent_message", "message": "done"}), flush=True)
if mode == "compact":
    print(json.dumps({"type": "context_compacted", "pre_compaction_tokens": 170000, "post_compaction_tokens": 43000, "tokens_removed": 127000}), flush=True)
if mode == "block":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    time.sleep(60)
print("done", flush=True)
"""


def stub_session(final: int = 0, mode: str = "done") -> CodexJsonSession:
    return CodexJsonSession(
        TurnStreamAsk(prompt="build it", cwd=Path("/tmp")),
        sandbox=IMPLEMENTER_SANDBOX,
        build_argv=lambda _ask, _sandbox: (
            sys.executable,
            "-c",
            STUB_CODEX,
            str(final),
            mode,
        ),
    )


async def collect(session: CodexJsonSession) -> list[Turn]:
    return [turn async for turn in session.turns()]


async def test_the_codex_json_session_streams_text_usage_and_compaction() -> None:
    session = stub_session(final=1_234, mode="compact")

    turns = await collect(session)

    assert turns == [TokenUsage(consumed_tokens=1_234), "done", "done\n"]
    assert session.auto_compactions == (
        AutoCompaction(
            event="compacted",
            pre_compaction_tokens=170_000,
            post_compaction_tokens=43_000,
            tokens_removed=127_000,
        ),
    )


async def test_kill_stops_the_codex_turn_stream_without_raising() -> None:
    session = stub_session(mode="block")
    reading = asyncio.create_task(collect(session))

    await asyncio.sleep(0.05)
    session.kill()
    turns = await asyncio.wait_for(reading, timeout=5.0)

    assert "done" in turns
    assert await session.wait() != 0


# ── the whole adapter, against a stub process ────────────────────────────────────────────────


async def test_a_session_records_completed_turn_consumption(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await codex_implementer_with_stub(final=1_234_567).run(session_context(wt))

    assert t.killed is None
    assert t.exit_code == 0
    assert not hasattr(t, "peak_context_tokens")
    assert t.consumed_tokens == 1_234_567


async def test_a_session_runs_to_completion_without_completed_turn_usage(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await codex_implementer_with_stub(final=0).run(session_context(wt))

    assert t.killed is None
    assert t.consumed_tokens == 0
    assert "done" in t.session_output
    assert classify_implementer(t, GREEN) is Outcome.IMPASSE


def codex_implementer_with_stub(final: int = 0) -> CodexImplementer:
    def open_session(ask: TurnStreamAsk) -> CodexJsonSession:
        return CodexJsonSession(
            ask,
            sandbox=IMPLEMENTER_SANDBOX,
            build_argv=lambda _ask, _sandbox: (
                sys.executable,
                "-c",
                STUB_CODEX,
                str(final),
                "done",
            ),
        )

    return CodexImplementer(open_session=open_session)


async def test_the_implementer_is_killed_through_the_codex_sdk_session(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await codex_implementer_with_blocking_stub().run(
        session_context(wt, budget=Budget(wall_clock_s=0.01))
    )

    assert t.killed == "wall-clock"
    assert t.exit_code != 0


def codex_implementer_with_blocking_stub() -> CodexImplementer:
    def open_session(ask: TurnStreamAsk) -> CodexJsonSession:
        return CodexJsonSession(
            ask,
            sandbox=IMPLEMENTER_SANDBOX,
            build_argv=lambda _ask, _sandbox: (
                sys.executable,
                "-c",
                STUB_CODEX,
                "0",
                "block",
            ),
        )

    return CodexImplementer(open_session=open_session)


# ── Codex as Editor ─────────────────────────────────────────────────────────────────────────

FAILURE = failure_report(
    Outcome.IMPASSE,
    telemetry(commits=0, impasse_report=impasse()),
    suite(green=True),
)


async def test_the_codex_editor_reuses_the_editor_core_under_read_only_sandbox(tmp_path: Path) -> None:
    seen: list[tuple[TurnStreamAsk, str]] = []

    def open_session(ask: TurnStreamAsk) -> CodexJsonSession:
        return CodexJsonSession(
            ask,
            sandbox=EDITOR_SANDBOX,
            build_argv=lambda _ask, sandbox: _recording_editor_argv(seen, _ask, sandbox),
        )

    editor = CodexEditor(open_session=open_session, suite=("uv", "run", "pytest", "-q"))
    t, verdict = await editor.adjudicate(
        SessionContext(
            brief=Brief(body="build it"),
            findings=Findings(body=""),
            worktree=Worktree(path=tmp_path, branch="ralph/01", base="integration"),
            budget=Budget(wall_clock_s=10.0),
        ),
        FAILURE,
        must_be_terminal=False,
    )

    assert seen[0][0].permit is None
    assert seen[0][1] == EDITOR_SANDBOX
    assert verdict is not None
    assert verdict.verdict is Verdict.PLANNING_DEFECT
    assert t.consumed_tokens == 55_000
    assert t.commits == 0
    assert classify_editor(t, verdict) is Outcome.SUCCESS


def _recording_editor_argv(
    seen: list[tuple[TurnStreamAsk, str]], ask: TurnStreamAsk, sandbox: str
) -> Sequence[str]:
    seen.append((ask, sandbox))
    answer = {
        "verdict": "planning-defect",
        "rationale": "the brief contradicts itself",
    }
    return (
        sys.executable,
        "-c",
        "import json; "
        "print(json.dumps({'type':'agent_message','message':%r})); "
        "print(json.dumps({'type':'turn.completed','usage':{'total_tokens':55000}}))"
        % f"<verdict>{json.dumps(answer)}</verdict>",
    )


# ── and once, for real ───────────────────────────────────────────────────────────────────────

REAL = pytest.mark.skipif(
    os.environ.get("RALPH_REAL_CODEX") != "1" or shutil.which("codex") is None,
    reason="set RALPH_REAL_CODEX=1, with codex on PATH, to spend real tokens",
)


@REAL
async def test_a_real_codex_session_lands_a_real_sub_issue(repo: TargetRepo) -> None:
    """The only test in the suite that calls a model."""
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    brief = Brief(
        body=textwrap.dedent("""\
            # 01 — multiply

            ## Acceptance criteria

            - [ ] `calculator.py` exports `multiply(a, b)` returning `a * b`
            - [ ] a test in `test_calculator.py` covers it, and the suite is green
            """)
    )

    t = await codex_implementer().run(
        session_context(wt, brief=brief, findings=Findings(body=""), budget=Budget(wall_clock_s=600.0))
    )

    assert t.killed is None
    assert t.commits >= 1
    assert not hasattr(t, "peak_context_tokens")
    assert t.consumed_tokens > 0
    assert repo.run_suite(wt.path)
