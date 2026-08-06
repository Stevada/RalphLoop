"""The Codex adapter: the argv it builds and the usage it reads at session end."""

from __future__ import annotations

import os
import asyncio
import json
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

import pytest

from ralph.adapters.codex import (
    codex_integrator,
    EDITOR_SANDBOX,
    IMPLEMENTER_SANDBOX,
    CodexUsageError,
    CodexEditor,
    CodexImplementer,
    CodexJsonSession,
    codex_argv,
    codex_implementer,
    end_of_turn_consumption,
)
from ralph.adapters.runtime.bounding import Bound
from ralph.adapters.git import GitCli
from ralph.adapters.runtime.prompt import integrator_prompt
from ralph.adapters.runtime.session import Session
from ralph.adapters.runtime.turn_stream import AutoCompaction, Turn, TurnStreamAsk
from ralph.harness import (
    NOTHING,
    Outcome,
    TokenConsumption,
    Verdict,
    classify_editor,
    classify_implementer,
    failure_report,
)
from ralph.issues import Findings, Spec
from ralph.ports import Budget, SessionContext, Worktree
from tests.builders import impasse, telemetry
from tests.testbed import TargetRepo

SPEC = Spec(body="# 01 — make it add\n\n## Acceptance criteria\n\n- [ ] `add(1, 2) == 3`")
FINDINGS = Findings(body="`add()` is already in calculator.py")
GENEROUS = Budget(wall_clock_s=30.0)


def session_context(
    wt: Worktree, spec: Spec = SPEC, findings: Findings = FINDINGS, budget: Budget = GENEROUS
) -> SessionContext:
    return SessionContext(spec=spec, findings=findings, worktree=wt, budget=budget)


# ── the argv ─────────────────────────────────────────────────────────────────────────────────


def test_the_session_is_asked_for_json() -> None:
    """`--json` is how the session reports completed-turn usage."""
    argv = codex_argv(SPEC, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="main"))

    assert argv[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert "--json" in argv
    assert "--cd" in argv and "/w" in argv


def test_the_spec_and_the_findings_both_reach_the_model() -> None:
    prompt = codex_argv(SPEC, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))[-1]

    assert "`add(1, 2) == 3`" in prompt
    assert "already in calculator.py" in prompt
    assert "<impasse>" in prompt


def test_how_codex_is_driven_is_fixed_not_configured() -> None:
    argv = codex_argv(SPEC, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))

    assert "--sandbox" in argv and IMPLEMENTER_SANDBOX in argv
    assert "--ask-for-approval" in argv and "never" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "gpt-5.5" in argv


# ── completed-turn usage ─────────────────────────────────────────────────────────────────────


async def test_consumption_comes_from_the_completed_turn_usage() -> None:
    session = Session(
        bound=Bound(killed=None, consumption=NOTHING),
        exit_code=0,
        output=(
            '{"type": "turn.completed", "usage": {"total_tokens": 123}}\n'
            '{"type": "turn.completed", "usage": {"total_tokens": 456}}\n'
        ),
        wall_clock_s=1.0,
    )

    # A total with no components is all this event says, and all the harness may claim.
    assert await end_of_turn_consumption(session, Worktree(Path("/w"), "b", "base")) == (
        TokenConsumption.total_only(456)
    )


async def test_consumption_splits_a_usage_that_carries_no_total() -> None:
    """Verbatim from codex-cli 0.143.0.

    Two claims. The total is input + output — 2_450_064, not the 4_729_086 that summing all four
    would give, because `cached_input_tokens` and `reasoning_output_tokens` break down the other
    two rather than adding to them.

    And the buckets are **disjoint**: Codex counts cached tokens inside `input_tokens`, so the
    fresh half is the difference. 2_421_295 reported as input is really 152_111 fresh prompt on top
    of a 2_269_184-token cache read — a 15x difference in what the same run costs, and the whole
    reason the buckets are reported separately.
    """
    session = Session(
        bound=Bound(killed=None, consumption=NOTHING),
        exit_code=0,
        output=(
            '{"type":"turn.completed","usage":{"input_tokens":2421295,'
            '"cached_input_tokens":2269184,"output_tokens":28769,'
            '"reasoning_output_tokens":9838}}\n'
        ),
        wall_clock_s=1.0,
    )

    consumption = await end_of_turn_consumption(session, Worktree(Path("/w"), "b", "base"))

    assert consumption == TokenConsumption.split(input=152_111, cache_read=2_269_184, output=28_769)
    assert consumption is not None and consumption.consumed_tokens == 2_450_064


async def test_a_completed_turn_without_usage_is_loud() -> None:
    session = Session(
        bound=Bound(killed=None, consumption=NOTHING),
        exit_code=0,
        output='{"type": "turn.completed", "usage": {}}\n',
        wall_clock_s=1.0,
    )

    with pytest.raises(CodexUsageError):
        await end_of_turn_consumption(session, Worktree(Path("/w"), "b", "base"))


# ── the JSONL turn stream ────────────────────────────────────────────────────────────────────

STUB_CODEX = """\
import json, signal, sys, time

final = int(sys.argv[1])
mode = sys.argv[2]
if final:
    print(json.dumps({"type": "thread.started", "thread_id": "codex-thread-123"}), flush=True)
    # The live shape: components, no total, with cached/reasoning set so that a reader which
    # summed all four would report 2x final rather than final.
    usage = {"input_tokens": final - 34, "cached_input_tokens": final - 34,
             "output_tokens": 34, "reasoning_output_tokens": 34}
    print(json.dumps({"type": "turn.completed", "usage": usage}), flush=True)
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

    # The stub's prompt is entirely cached, so `input` is nothing and the 1_200 is all cache read.
    assert turns == [
        TokenConsumption.split(input=0, cache_read=1_200, output=34),
        "done",
        "done\n",
    ]
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
    assert t.consumption.consumed_tokens == 1_234_567
    assert t.resumable_identifier == "codex-thread-123"


async def test_a_session_runs_to_completion_without_completed_turn_usage(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await codex_implementer_with_stub(final=0).run(session_context(wt))

    assert t.killed is None
    assert t.consumption.consumed_tokens == 0
    assert "done" in t.session_output
    assert classify_implementer(t) is Outcome.IMPASSE


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


async def test_the_codex_integrator_opens_a_fresh_session_in_the_conflicted_worktree(
    repo: TargetRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    argv_log = tmp_path / "codex-argv.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        textwrap.dedent(f"""\
            #!{sys.executable}
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            Path(os.environ["RALPH_CODEX_ARGV_LOG"]).write_text(json.dumps(sys.argv[1:]))
            Path("conflict.txt").write_text("resolved\\n")
            subprocess.run(["git", "add", "-A"], check=True)
            subprocess.run([
                "git",
                "-c", "user.email=codex@ralph.invalid",
                "-c", "user.name=Codex Stub",
                "commit",
                "-m", "resolve conflict",
            ], check=True)
            print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 736, "cached_input_tokens": 700, "output_tokens": 34, "reasoning_output_tokens": 12}}}}), flush=True)
            print(json.dumps({{"type": "agent_message", "message": "resolved"}}), flush=True)
            """)
    )
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RALPH_CODEX_ARGV_LOG", str(argv_log))

    context = session_context(wt)
    t = await codex_integrator().reconcile(context)

    argv = json.loads(argv_log.read_text())
    assert argv[:3] == ["--ask-for-approval", "never", "exec"]
    assert "--json" in argv
    assert "--sandbox" in argv and IMPLEMENTER_SANDBOX in argv, "it must be able to write"
    assert "--cd" in argv and str(wt.path) in argv
    assert "resume" not in argv, "a fresh session: the conflict is in the repo, not in a transcript"
    assert argv[-1] == integrator_prompt(context.spec, context.findings)
    assert t.killed is None
    assert t.resumable_identifier is None
    assert t.consumption.consumed_tokens == 770
    assert t.commits == 1
    assert "resolved" in t.session_output


# ── Codex as Editor ─────────────────────────────────────────────────────────────────────────

FAILURE = failure_report(
    Outcome.IMPASSE,
    telemetry(commits=0, impasse_report=impasse()),
)


async def test_the_codex_editor_reuses_the_editor_core_under_read_only_sandbox(
    tmp_path: Path,
) -> None:
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
            spec=Spec(body="build it"),
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
    assert t.consumption.consumed_tokens == 55_000
    assert t.commits == 0
    assert classify_editor(t, verdict) is Outcome.SUCCESS


def _recording_editor_argv(
    seen: list[tuple[TurnStreamAsk, str]], ask: TurnStreamAsk, sandbox: str
) -> Sequence[str]:
    seen.append((ask, sandbox))
    answer = {
        "verdict": "planning-defect",
        "rationale": "the spec contradicts itself",
    }
    return (
        sys.executable,
        "-c",
        "import json; "
        "print(json.dumps({'type':'agent_message','message':%r})); "
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':54000,"
        "'cached_input_tokens':50000,'output_tokens':1000,'reasoning_output_tokens':400}}))"
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
    spec = Spec(
        body=textwrap.dedent("""\
            # 01 — multiply

            ## Acceptance criteria

            - [ ] `calculator.py` exports `multiply(a, b)` returning `a * b`
            - [ ] a test in `test_calculator.py` covers it, and the suite is green
            """)
    )

    t = await codex_implementer().run(
        session_context(
            wt, spec=spec, findings=Findings(body=""), budget=Budget(wall_clock_s=600.0)
        )
    )

    assert t.killed is None
    assert t.commits >= 1
    assert not hasattr(t, "peak_context_tokens")
    assert t.consumption.consumed_tokens > 0
    assert repo.run_suite(wt.path)


@REAL
async def test_a_real_codex_integrator_resolves_a_real_merge_conflict(
    repo: TargetRepo,
) -> None:
    """Spends two real Codex turns: one to create the work, one to reconcile it after the base
    moved. The second is a *fresh* session — it is told nothing about the first."""
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    spec = Spec(
        body=textwrap.dedent("""\
            # 01 — change shared marker

            ## Acceptance criteria

            - [ ] `shared.py` sets `MARKER = "codex-side"`
            - [ ] the change is committed
            """)
    )
    context = session_context(
        wt, spec=spec, findings=Findings(body=""), budget=Budget(wall_clock_s=900.0)
    )

    first = await codex_implementer().run(context)
    assert first.resumable_identifier is not None
    assert first.commits >= 1

    (repo.path / "shared.py").write_text('MARKER = "integration-side"\n')
    repo.git("add", "shared.py")
    repo.git("commit", "-m", "move integration marker")
    merge = subprocess.run(
        ["git", "merge", "--no-edit", "integration"],
        cwd=wt.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge.returncode != 0
    assert "<<<<<<<" in (wt.path / "shared.py").read_text()

    reconciled = await codex_integrator().reconcile(context)

    assert reconciled.killed is None
    assert reconciled.consumption.consumed_tokens > 0
    assert git.merge_finished(wt), "it resolved the conflict but never committed it"
    assert repo.run_suite(wt.path)
