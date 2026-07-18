"""The Codex adapter: the argv it builds and the usage it reads at session end."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from ralph.adapters.codex import (
    CodexUsageError,
    codex_argv,
    codex_implementer,
    end_of_turn_consumed_tokens,
)
from ralph.adapters.context import Bound
from ralph.adapters.git import GitCli
from ralph.adapters.session import Session, SubprocessImplementer
from ralph.harness import Outcome, SuiteResult, classify_implementer
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext, Worktree
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

    assert argv[:3] == ["codex", "exec", "--json"]


def test_the_brief_and_the_findings_both_reach_the_model() -> None:
    prompt = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))[-1]

    assert "`add(1, 2) == 3`" in prompt
    assert "already in calculator.py" in prompt
    assert "<impasse>" in prompt


def test_how_codex_is_driven_is_fixed_not_configured() -> None:
    argv = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))

    assert "--sandbox" in argv and "workspace-write" in argv
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


# ── the whole adapter, against a stub process ────────────────────────────────────────────────

STUB_CODEX = """\
import json, sys

final = int(sys.argv[1])
if final:
    print(json.dumps({"type": "turn.completed", "usage": {"total_tokens": final}}), flush=True)
print("done", flush=True)
"""


def stub_implementer(final: int = 0) -> SubprocessImplementer:
    real = codex_implementer()
    return SubprocessImplementer(
        build_argv=lambda brief, findings, wt: (
            sys.executable,
            "-c",
            STUB_CODEX,
            str(final),
        ),
        final_consumed_tokens=real.final_consumed_tokens,
    )


async def test_a_session_records_completed_turn_consumption(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await stub_implementer(final=1_234_567).run(session_context(wt))

    assert t.killed is None
    assert t.exit_code == 0
    assert not hasattr(t, "peak_context_tokens")
    assert t.consumed_tokens == 1_234_567


async def test_a_session_runs_to_completion_without_completed_turn_usage(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await stub_implementer(final=0).run(session_context(wt))

    assert t.killed is None
    assert t.consumed_tokens == 0
    assert "done" in t.session_output
    assert classify_implementer(t, GREEN) is Outcome.IMPASSE


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
