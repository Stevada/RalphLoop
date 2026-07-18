"""The Codex adapter: the argv it builds, and the rollout file it tails.

Exactly one test in this file — and in the whole suite — calls a model, and it is skipped unless
you ask for it. Everything else drives the adapter with a **fixture rollout file** and a **stub
process**: a python script that prints what Codex prints and writes what Codex writes. That is
enough to test everything the adapter actually does, because everything the adapter actually does
is read those two streams.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from ralph.adapters.codex import (
    CodexContextSource,
    RolloutParseError,
    codex_argv,
    codex_implementer,
    codex_sessions_dir,
    parse_observation,
    thread_id,
)
from ralph.adapters.git import GitCli
from ralph.adapters.session import SubprocessImplementer, Transcript
from ralph.harness import Outcome, SuiteResult, classify_implementer
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext, Worktree
from tests.testbed import TargetRepo

BRIEF = Brief(body="# 01 — make it add\n\n## Acceptance criteria\n\n- [ ] `add(1, 2) == 3`")
FINDINGS = Findings(body="`add()` is already in calculator.py")
GENEROUS = Budget(wall_clock_s=30.0)


def session_context(
    wt: Worktree, brief: Brief = BRIEF, findings: Findings = FINDINGS, budget: Budget = GENEROUS
) -> SessionContext:
    return SessionContext(brief=brief, findings=findings, worktree=wt, budget=budget)

GREEN = SuiteResult(green=True, output="", duration_s=0.0)


def token_count(context: int, consumed: int, used_percent: float | None = 0.0) -> str:
    """A `token_count` event exactly as Codex writes one. `context` and `consumed` are deliberately
    free to diverge — in the real file they always do, and the whole question is which is read."""
    payload: dict[str, object] = {
        "type": "token_count",
        "info": {
            "last_token_usage": {"input_tokens": context},
            "total_token_usage": {"total_tokens": consumed},
            "model_context_window": 272_000,
        },
    }
    if used_percent is not None:
        payload["rate_limits"] = {"primary": {"used_percent": used_percent}}
    return json.dumps({"type": "event_msg", "payload": payload})


def rollout(dir: Path, id: str, *events: str) -> Path:
    """A rollout file where Codex would have put one: under a dated directory, named for the
    thread. The name is the whole of the lookup, so the name is what the test fixes."""
    day = dir / "2026-07-13"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-07-13T09-15-00-{id}.jsonl"
    path.write_text("".join(f"{e}\n" for e in events))
    return path


# ── the argv ─────────────────────────────────────────────────────────────────────────────────


def test_the_session_is_asked_for_json() -> None:
    """Not a preference. `--json` is how the session announces the `thread_id` that finds its
    rollout file — and without that file there is no context signal and no ceiling at all."""
    argv = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="main"))

    assert argv[:3] == ["codex", "exec", "--json"]


def test_the_brief_and_the_findings_both_reach_the_model() -> None:
    prompt = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))[-1]

    assert "`add(1, 2) == 3`" in prompt  # the brief
    assert "already in calculator.py" in prompt  # what an earlier cycle learned
    assert "<impasse>" in prompt  # and how to say it cannot be done


def test_how_codex_is_driven_is_fixed_not_configured() -> None:
    """How `codex exec` runs is hardcoded, not a tuning surface: the sandbox and approval flags are
    always passed — never the bypass, which Codex rejects alongside them — and the model is fixed."""
    argv = codex_argv(BRIEF, FINDINGS, Worktree(path=Path("/w"), branch="ralph/01", base="m"))

    assert "--sandbox" in argv and "workspace-write" in argv
    assert "--ask-for-approval" in argv and "never" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "gpt-5.3-codex" in argv


def test_the_sessions_dir_follows_codex_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "/somewhere/else")

    assert codex_sessions_dir() == Path("/somewhere/else/sessions")


# ── the parse ────────────────────────────────────────────────────────────────────────────────


def test_it_reads_the_context_and_not_the_consumption() -> None:
    """The two numbers sit beside each other in the same event and are named almost the same. This
    is the assertion that fixes which one the ceiling is on: 16,802 is what the model was reasoning
    over; 133,410 is what the session has spent. A ceiling on the second is not a worse ceiling —
    it is a ceiling on session *length*, which is what the wall clock is already for.
    """
    o = parse_observation(token_count(context=16_802, consumed=133_410))

    assert o is not None
    assert o.context_tokens == 16_802
    assert o.consumed_tokens == 133_410


def test_it_reads_the_rate_limit() -> None:
    o = parse_observation(token_count(1, 1, used_percent=61.5))

    assert o is not None
    assert o.rate_limit_used_percent == 61.5


def test_a_rate_limit_codex_did_not_send_is_none_not_zero() -> None:
    """Zero would read as "plenty left" — the most dangerous possible reading of "we don't know"."""
    o = parse_observation(token_count(1, 1, used_percent=None))

    assert o is not None
    assert o.rate_limit_used_percent is None


@pytest.mark.parametrize(
    "line",
    [
        '{"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}}',
        '{"type": "response_item", "payload": {"type": "function_call"}}',
        "Reading prompt from stdin...",  # Codex prints human lines alongside its JSON
        '{"type": "event_msg", "payload": {"type": "token_count", "info": null}}',
    ],
    ids=["another event", "not an event", "not even json", "no usage yet"],
)
def test_everything_that_is_not_a_token_count_is_not_an_observation(line: str) -> None:
    assert parse_observation(line) is None


def test_a_token_count_it_cannot_read_is_fatal() -> None:
    """The alternative is a ceiling that silently never fires — worse than no ceiling at all, since
    the harness would go on reporting a peak of zero for sessions that left the smart zone hours
    ago. If Codex changes the shape of this event, the run should stop, loudly."""
    with pytest.raises(RolloutParseError):
        parse_observation(
            '{"type": "event_msg", "payload": {"type": "token_count", "info": {"tokens": 9}}}'
        )


def test_the_thread_id_is_read_from_the_first_line_of_stdout() -> None:
    assert thread_id('{"type": "thread.started", "thread_id": "0199abc"}') == "0199abc"
    assert thread_id('{"type": "turn.completed", "usage": {}}') is None
    assert thread_id("codex 0.5.0") is None


# ── the source ───────────────────────────────────────────────────────────────────────────────


async def observed(source: CodexContextSource) -> list[int]:
    return [o.context_tokens async for o in source.observations()]


async def test_it_tails_this_sessions_rollout_file_and_never_a_siblings(tmp_path: Path) -> None:
    """Four Codex sessions run at once by default, each with its own rollout file in the same
    directory. Metering a sibling's file would kill the wrong session — and the harness would
    report `ceiling-exceeded` against a sub-issue that had never left the smart zone.
    """
    rollout(tmp_path, "mine", token_count(30_000, 40_000))
    rollout(tmp_path, "a-sibling", token_count(200_000, 900_000))

    transcript = Transcript()
    transcript.append('{"type": "thread.started", "thread_id": "mine"}\n')
    transcript.close()

    source = CodexContextSource(transcript=transcript, sessions_dir=tmp_path, poll_s=0.01)

    assert await observed(source) == [30_000]


async def test_it_yields_every_model_call_in_order(tmp_path: Path) -> None:
    rollout(
        tmp_path,
        "t1",
        token_count(10_000, 10_000),
        '{"type": "event_msg", "payload": {"type": "agent_message"}}',
        token_count(40_000, 55_000),
        token_count(90_000, 140_000),
    )
    transcript = Transcript()
    transcript.append('{"type": "thread.started", "thread_id": "t1"}\n')
    transcript.close()

    source = CodexContextSource(transcript=transcript, sessions_dir=tmp_path, poll_s=0.01)

    assert await observed(source) == [10_000, 40_000, 90_000]


async def test_it_yields_while_the_session_is_still_running(tmp_path: Path) -> None:
    """The point of the whole file-tailing exercise. `codex exec --json` only reports usage at
    session end — by which time a session that left the smart zone has already spent an hour
    reasoning badly and committing the results."""
    path = rollout(tmp_path, "t1", token_count(10_000, 10_000))
    transcript = Transcript()
    transcript.append('{"type": "thread.started", "thread_id": "t1"}\n')

    source = CodexContextSource(transcript=transcript, sessions_dir=tmp_path, poll_s=0.01)
    seen: list[int] = []

    async def keep_working() -> None:
        await asyncio.sleep(0.05)
        with path.open("a") as f:
            f.write(f"{token_count(130_000, 200_000)}\n")
        await asyncio.sleep(0.05)
        transcript.close()  # stdout EOF: the session has said its last word

    worker = asyncio.create_task(keep_working())
    async for o in source.observations():
        seen.append(o.context_tokens)
        if len(seen) == 2:
            assert not transcript.closed.is_set()  # observed *while it ran*, not after
    await worker

    assert seen == [10_000, 130_000]


async def test_a_session_that_dies_before_it_says_who_it_is_runs_unmetered_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Codex crashing on startup leaves no thread id and no rollout file. That is not a parse
    error — it is a session with nothing to meter, and the classifier will call it what it is from
    the exit code and the commit count. But it must not pass silently, so it warns."""
    caplog.set_level("WARNING", logger="ralph.adapters.codex")
    transcript = Transcript()
    transcript.append("error: could not read config\n")
    transcript.close()

    source = CodexContextSource(transcript=transcript, sessions_dir=tmp_path, poll_s=0.01)

    assert await observed(source) == []
    assert "unmetered" in caplog.text


# ── the whole adapter, against a stub process ────────────────────────────────────────────────

STUB_CODEX = """\
import json, os, pathlib, sys, time

thread = "stub-thread"
print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)

day = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions" / "2026-07-13"
day.mkdir(parents=True, exist_ok=True)
rollout = day / f"rollout-2026-07-13T09-15-00-{thread}.jsonl"

for context in [int(n) for n in sys.argv[1].split(",")]:
    with rollout.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": context},
            "total_token_usage": {"total_tokens": context * 4},
        }, "rate_limits": {"primary": {"used_percent": 3.0}}}}) + "\\n")
    print(json.dumps({"type": "item.completed"}), flush=True)
    time.sleep(0.1)

final = int(sys.argv[2])
if final:
    print(json.dumps({"type": "turn.completed", "usage": {"total_tokens": final}}), flush=True)
print("done", flush=True)
"""
"""A stub that behaves like Codex where it matters: it announces a thread id on stdout, and it
appends `token_count` events to the rollout file named after that thread, as it goes."""


def stub_implementer(contexts: str, final: int = 0) -> SubprocessImplementer:
    """The real adapter — real source, real tail, real kill loop — around a fake model."""
    real = codex_implementer()
    return SubprocessImplementer(
        build_argv=lambda brief, findings, wt: (
            sys.executable,
            "-c",
            STUB_CODEX,
            contexts,
            str(final),
        ),
        context=real.context,
        final_consumed_tokens=real.final_consumed_tokens,
    )


async def test_a_session_that_stays_in_the_smart_zone_is_left_alone(
    repo: TargetRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await stub_implementer("20000,60000,90000").run(session_context(wt))

    assert t.killed is None
    assert t.exit_code == 0
    assert t.peak_context_tokens == 90_000
    assert t.consumed_tokens == 360_000  # four times the context, and gated on not at all


async def test_consumption_comes_from_the_end_of_turn_usage(
    repo: TargetRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await stub_implementer("20000,60000,90000", final=1_234_567).run(session_context(wt))

    assert t.peak_context_tokens == 90_000
    assert t.consumed_tokens == 1_234_567


async def test_a_session_that_leaves_it_is_killed_mid_flight(
    repo: TargetRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub would go on to a fourth model call and print `done`. It never gets there: the
    harness reads 130k out of the rollout file *while the process is alive* and kills it."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await stub_implementer("20000,130000,140000").run(session_context(wt))

    assert t.killed == "ceiling"
    assert t.peak_context_tokens >= 120_000
    assert "done" not in t.session_output  # it did not live to finish
    assert classify_implementer(t, GREEN) is Outcome.CEILING_EXCEEDED


# ── and once, for real ───────────────────────────────────────────────────────────────────────

REAL = pytest.mark.skipif(
    os.environ.get("RALPH_REAL_CODEX") != "1" or shutil.which("codex") is None,
    reason="set RALPH_REAL_CODEX=1, with codex on PATH, to spend real tokens",
)


@REAL
async def test_a_real_codex_session_lands_a_real_sub_issue(repo: TargetRepo) -> None:
    """**The only test in the suite that calls a model.** Everything above proves the adapter reads
    Codex correctly; this proves it was reading Codex.

    It asserts nothing about the model's cleverness — only that a real `codex exec` produced real
    commits, that the suite went green, and that the harness metered a real context out of a real
    rollout file. If `peak_context_tokens` came back zero, the ceiling was never watching.
    """
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
    assert t.peak_context_tokens > 0  # a real rollout file was really tailed
    assert repo.run_suite(wt.path)
