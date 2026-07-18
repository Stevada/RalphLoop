"""The Copilot adapter: SDK-backed Implementer and Editor.

**No test here runs the real `copilot` binary or starts a live SDK session.** Both actors are driven
through the SDK seam with stub sessions, so no test here depends on a model answering.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path

from ralph.adapters.copilot import CopilotEditor, CopilotImplementer
from ralph.adapters.editor import TurnStreamAsk, Permission, TokenUsage, Turn
from ralph.adapters.git import GitCli, run_git
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

SUITE: Sequence[str] = ("python", "-m", "pytest")

FAILURE = failure_report(
    Outcome.IMPASSE, telemetry(commits=0, impasse_report=impasse()), suite(green=True)
)

BRIEF = Brief(body="# 01 — build it\n\n## Acceptance criteria\n\n- [ ] Add a file.")
FINDINGS = Findings(body="Start from the existing calculator module.")


def _worktree(tmp_path: Path) -> Worktree:
    path = tmp_path / ".worktrees" / "active" / "02-thing"
    path.mkdir(parents=True)
    return Worktree(path=path, branch="ralph/02-thing", base="integration")


# ── the whole Implementer, against a stub SDK session ────────────────────────────────────────


class StubTurnStreamSession:
    def __init__(
        self,
        turns: Sequence[Turn],
        *,
        on_start: Callable[[], None] | None = None,
        block_after_turns: bool = False,
    ) -> None:
        self._turns = turns
        self._on_start = on_start
        self._block_after_turns = block_after_turns
        self._released = asyncio.Event()
        self.returncode: int | None = None
        self.killed = False

    async def turns(self) -> AsyncGenerator[Turn, None]:
        if self._on_start is not None:
            self._on_start()
        for turn in self._turns:
            if self.killed:
                return
            yield turn
        if self._block_after_turns:
            await self._released.wait()
            if self.killed:
                return
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._released.set()

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else -1


async def test_the_implementer_reports_sdk_usage_and_git_facts(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    seen: list[TurnStreamAsk] = []

    def commit_work() -> None:
        (wt.path / "copilot.txt").write_text("sdk implementer\n")
        run_git(wt.path, "add", "copilot.txt")
        run_git(
            wt.path,
            "-c",
            "user.email=copilot@ralph.invalid",
            "-c",
            "user.name=Copilot Stub",
            "commit",
            "-m",
            "copilot sdk change",
        )

    def open_session(ask: TurnStreamAsk) -> StubTurnStreamSession:
        seen.append(ask)
        return StubTurnStreamSession(
            ["implemented\n", TokenUsage(consumed_tokens=999_999)],
            on_start=commit_work,
        )

    t = await CopilotImplementer(open_session=open_session).run(_context(wt))

    assert seen[0].cwd == wt.path
    assert "# 01 — build it" in seen[0].prompt
    assert "Commit your work" in seen[0].prompt
    assert "<impasse>" in seen[0].prompt
    assert "Start from the existing calculator module." in seen[0].prompt
    assert seen[0].permit is not None
    assert seen[0].permit("Write", {"file_path": "copilot.txt"}).allowed
    assert t.killed is None
    assert t.exit_code == 0
    assert t.consumed_tokens == 999_999
    assert t.commits == 1
    assert "copilot.txt" in t.diffstat
    assert t.session_output == "implemented\n"


async def test_the_implementer_parses_an_impasse_from_the_sdk_output(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    body = {
        "failing_test": "test_missing_api",
        "assertion_output": "module has no widget()",
        "approaches": [
            {
                "tried": "searched for widget",
                "abandoned_because": "the API does not exist",
            }
        ],
        "unsatisfiable_criterion": "Call widget().",
        "what_would_satisfy": "A brief that names an existing API.",
    }

    def open_session(_ask: TurnStreamAsk) -> StubTurnStreamSession:
        return StubTurnStreamSession([f"<impasse>{json.dumps(body)}</impasse>"])

    t = await CopilotImplementer(open_session=open_session).run(_context(wt))

    assert t.impasse_report is not None
    assert t.impasse_report.what_would_satisfy == "A brief that names an existing API."


async def test_the_implementer_is_killed_through_the_sdk_session(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    session = StubTurnStreamSession([], block_after_turns=True)

    t = await CopilotImplementer(open_session=lambda _ask: session).run(
        _context(wt, budget=Budget(wall_clock_s=0.01))
    )

    assert t.killed == "wall-clock"
    assert session.killed
    assert t.exit_code == -9


def _context(wt: Worktree, budget: Budget = Budget(wall_clock_s=20.0)) -> SessionContext:
    return SessionContext(brief=BRIEF, findings=FINDINGS, worktree=wt, budget=budget)


# ── the whole Editor, against a stub SDK session ─────────────────────────────────────────────


async def test_the_editor_runs_to_completion(tmp_path: Path) -> None:
    telemetry, verdict = await _adjudicate(tmp_path, consumed_tokens=130_000, verdict=None)

    assert telemetry.killed is None
    assert not hasattr(telemetry, "peak_context_tokens")
    assert verdict is None


async def test_the_editor_reports_no_commits_by_construction(tmp_path: Path) -> None:
    """Not observed — *constructed*. It was denied every tool that could have made one."""
    telemetry, verdict = await _adjudicate(tmp_path, consumed_tokens=9_000, verdict=Verdict.REVISE)

    assert telemetry.commits == 0
    assert telemetry.diffstat == ""
    assert telemetry.killed is None
    assert verdict is not None and verdict.verdict is Verdict.REVISE


async def test_the_editor_reads_the_verdict_out_of_the_sdk_session(tmp_path: Path) -> None:
    _, verdict = await _adjudicate(
        tmp_path, consumed_tokens=9_000, verdict=Verdict.PLANNING_DEFECT
    )

    assert verdict is not None
    assert verdict.verdict is Verdict.PLANNING_DEFECT
    assert verdict.rationale.startswith("the brief assumed")


async def test_the_prompt_and_worktree_reach_the_sdk_session(tmp_path: Path) -> None:
    seen: list[TurnStreamAsk] = []
    await _adjudicate(tmp_path, consumed_tokens=9_000, verdict=Verdict.REVISE, seen=seen)

    assert "You are the **Editor**" in seen[0].prompt
    assert "build it" in seen[0].prompt  # the brief the Implementer was given
    assert seen[0].cwd == tmp_path / ".worktrees" / "active" / "02-thing"


async def test_the_editor_denies_mutating_tools_through_the_shared_permit(tmp_path: Path) -> None:
    denied: list[Permission] = []

    def open_session(ask: TurnStreamAsk) -> StubTurnStreamSession:
        assert ask.permit is not None
        denied.extend(
            [
                ask.permit("Write", {"file_path": "calculator.py"}),
                ask.permit("Bash", {"command": "git commit -m fixed"}),
                ask.permit("Bash", {"command": "git cherry-pick abc123"}),
            ]
        )
        return StubTurnStreamSession([])

    await CopilotEditor(open_session=open_session, suite=SUITE).adjudicate(
        SessionContext(
            brief=Brief(body="build it"),
            findings=Findings(body=""),
            worktree=_worktree(tmp_path),
            budget=Budget(wall_clock_s=20.0),
        ),
        FAILURE,
        must_be_terminal=False,
    )

    assert [permission.allowed for permission in denied] == [False, False, False]


async def _adjudicate(
    tmp_path: Path,
    *,
    consumed_tokens: int,
    verdict: Verdict | None,
    seen: list[TurnStreamAsk] | None = None,
) -> tuple[SessionTelemetry, EditorVerdict | None]:
    """The whole Editor, against a stub session through the same seam `cli.py` wires."""
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

    def open_session(ask: TurnStreamAsk) -> StubTurnStreamSession:
        if seen is not None:
            seen.append(ask)
        turns: list[Turn] = []
        if said:
            turns.append(said)
        turns.append(TokenUsage(consumed_tokens=consumed_tokens))
        return StubTurnStreamSession(turns)

    return await CopilotEditor(open_session=open_session, suite=SUITE).adjudicate(
        SessionContext(
            brief=Brief(body="build it"),
            findings=Findings(body=""),
            worktree=_worktree(tmp_path),
            budget=Budget(wall_clock_s=20.0),
        ),
        FAILURE,
        must_be_terminal=False,
    )
